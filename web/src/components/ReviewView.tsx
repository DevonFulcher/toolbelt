// The review view: the files changed between two revisions of a repo, each as
// difftastic's structural diff in a read-only merge view. Hover shows what a
// language server says about the code under the pointer; Cmd/Ctrl-click opens
// its definition below. Everything is read from git when asked for: nothing
// here is pushed by the server.
import { MergeView } from "@codemirror/merge";
import { EditorView } from "@codemirror/view";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  approveAllFiles,
  decideFile,
  fetchPersonalReview,
  fetchReviewFile,
  fetchReviewFiles,
  fetchReviewText,
  type ReviewRevisions,
  type ReviewSide,
} from "../api/client";
import type { PersonalReviewState, ReviewChangedFile, ReviewDefinitionTarget, ReviewFile } from "../api/generated";
import { buildChanges } from "../logic/diffChanges";
import type { Counts } from "../store/selectors";
import { useApp, useAppStore } from "../store/StoreContext";
import { FilterBar } from "./FilterBar";
import { lspExtensions, readOnlyExtensions } from "./reviewEditor";
import "./review.css";

type Load<T> = { state: "ready"; data: T } | { state: "error"; message: string };

/** What `load` returns for `key`, `null` while it is on its way. */
function useLoad<T>(key: string | null, load: () => Promise<T>): Load<T> | null {
  const [loaded, setLoaded] = useState<{ key: string; result: Load<T> } | null>(null);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });
  useEffect(() => {
    if (key === null) return;
    let current = true;
    loadRef
      .current()
      .then((data) => current && setLoaded({ key, result: { state: "ready", data } }))
      .catch(
        (failure: unknown) =>
          current &&
          setLoaded({
            key,
            result: { state: "error", message: failure instanceof Error ? failure.message : String(failure) },
          }),
      );
    return () => {
      current = false;
    };
  }, [key]);
  return loaded !== null && loaded.key === key ? loaded.result : null;
}

const normalizeLineEndings = (text: string): string => text.replace(/\r\n?/g, "\n");

function DiffPane({
  revisions,
  diff,
  onDefinition,
  onError,
}: {
  revisions: ReviewRevisions;
  diff: ReviewFile;
  onDefinition: (side: ReviewSide, target: ReviewDefinitionTarget) => void;
  onError: (message: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const parent = host.current;
    if (parent === null) return;
    const { old_text: a, new_text: b, difftastic } = diff;
    const oldPath = diff.old_path ?? diff.path;
    const extensions = (side: ReviewSide, path: string) => [
      readOnlyExtensions(path),
      // Only files a configured language server handles have hover and definitions.
      diff.language_server === null
        ? []
        : lspExtensions({ revisions, side, path, onDefinition: (t) => onDefinition(side, t), onError }),
    ];
    const view = new MergeView({
      a: { doc: a, extensions: extensions("old", oldPath) },
      b: { doc: b, extensions: extensions("new", diff.path) },
      parent,
      highlightChanges: true,
      gutter: true,
      collapseUnchanged: { margin: 3, minSize: 4 },
      // CodeMirror passes its own "\n"-normalized copies of the texts to `override`.
      diffConfig: {
        override: (x, y) =>
          x === normalizeLineEndings(a) && y === normalizeLineEndings(b) ? buildChanges(difftastic, a, b) : [],
      },
    });
    return () => view.destroy();
  }, [revisions, diff, onDefinition, onError]);
  return <div className="review-diff" ref={host} />;
}

/** The file a definition is in, with the definition's line in view. */
function DefinitionPanel({
  revisions,
  side,
  target,
  onClose,
}: {
  revisions: ReviewRevisions;
  side: ReviewSide;
  target: ReviewDefinitionTarget;
  onClose: () => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const path = target.path;
  const text = useLoad(
    path === null ? null : `${side}:${path}`,
    () => fetchReviewText(revisions, side, path ?? ""),
  );
  useEffect(() => {
    const parent = host.current;
    if (parent === null || text === null || text.state !== "ready" || path === null) return;
    const view = new EditorView({ doc: text.data, extensions: readOnlyExtensions(path), parent });
    const line = view.state.doc.line(Math.min(target.line + 1, view.state.doc.lines));
    view.dispatch({
      selection: { anchor: line.from + target.character },
      effects: EditorView.scrollIntoView(line.from, { y: "center" }),
    });
    return () => view.destroy();
  }, [text, path, target]);
  return (
    <section className="review-definition">
      <div className="review-definition-bar">
        <span>
          {path === null ? `${target.uri} (outside the checkout)` : `${path}:${target.line + 1} (${side} side)`}
        </span>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>
      {text !== null && text.state === "error" ? <div className="empty error">{text.message}</div> : null}
      <div ref={host} className="review-definition-body" />
    </section>
  );
}

const FILE_GLYPH = { unreviewed: "", approved: "✓", changes_requested: "✎" } as const;

/** The branch has been validated and awaits your review, file by file: approve a
 * file's diff, or request changes to it with a note. A decision is for that
 * file's diff, so a new commit elsewhere keeps it and any change to the file
 * drops it. Approving the last file opens the PR (and posts it in Slack when
 * asked); "Approve all" approves each file's current diff. */
export function PersonalReviewBar({
  state,
  selected,
  onDone,
}: {
  state: PersonalReviewState;
  /** The file the Review view shows (``null``: none picked). */
  selected: string | null;
  onDone: () => void;
}) {
  const [note, setNote] = useState("");
  const [slack, setSlack] = useState(false);
  const [busy, setBusy] = useState<"approve" | "changes" | "all" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const file = state.files.find((f) => f.path === selected) ?? null;
  const approved = state.files.filter((f) => f.state === "approved").length;
  const run = (kind: "approve" | "changes" | "all", send: Promise<void>): void => {
    setBusy(kind);
    setProblem(null);
    send
      .then(() => {
        setNote("");
        onDone();
      })
      .catch((failure: unknown) => setProblem(failure instanceof Error ? failure.message : String(failure)))
      .finally(() => setBusy(null));
  };
  const decide = (kind: "approve" | "changes", target: NonNullable<typeof file>): void =>
    run(
      kind,
      decideFile({
        id: state.id,
        path: target.path,
        diff_hash: target.diff_hash,
        state: kind === "approve" ? "approved" : "changes_requested",
        note,
        post_in_slack: slack,
      }),
    );
  return (
    <section className="review-verdict" aria-label="Personal review">
      <div className="review-verdict-head">
        <strong>Personal review</strong>
        <span className="muted">{` · ${approved} of ${state.files.length} files approved`}</span>
        {state.result !== null && (
          <span className="muted">
            {` · the validate run would ${state.result === "pass" ? "approve" : "ask for changes"}`}
            {state.summary ? `: ${state.summary}` : ""}
          </span>
        )}
      </div>
      {state.opening_pr ? (
        <div>Every file is approved: pushing the branch and opening its PR…</div>
      ) : (
        <>
          {file !== null && file.state !== "unreviewed" && (
            <div className="muted">
              {file.state === "approved" ? `${file.path}: approved.` : `${file.path}: changes requested: ${file.note || "(no note)"}`}
            </div>
          )}
          <textarea
            aria-label="Note"
            placeholder="A note (required to request changes to a file)"
            value={note}
            rows={2}
            onChange={(event) => setNote(event.target.value)}
          />
          <div className="review-verdict-actions">
            <button type="button" disabled={busy !== null || file === null} onClick={() => file && decide("approve", file)}>
              {busy === "approve" ? "Approving…" : file ? `Approve ${file.path}` : "Pick a file to approve"}
            </button>
            <button
              type="button"
              disabled={busy !== null || file === null || note.trim() === ""}
              onClick={() => file && decide("changes", file)}
            >
              {busy === "changes" ? "Requesting…" : "Request changes"}
            </button>
            <button
              type="button"
              disabled={busy !== null || state.files.length === 0}
              onClick={() =>
                run(
                  "all",
                  approveAllFiles({
                    id: state.id,
                    files: Object.fromEntries(state.files.map((f) => [f.path, f.diff_hash])),
                    note,
                    post_in_slack: slack,
                  }),
                )
              }
            >
              {busy === "all" ? "Approving…" : "Approve all"}
            </button>
            {state.can_post_in_slack && (
              <label>
                <input type="checkbox" checked={slack} onChange={(event) => setSlack(event.target.checked)} /> Post in Slack once
                it is open
              </label>
            )}
          </div>
        </>
      )}
      {(problem ?? state.notice) !== null && <div className="error">{problem ?? state.notice}</div>}
    </section>
  );
}

const STATUS_TITLE = { A: "Added", M: "Modified", D: "Deleted", R: "Renamed" } as const;

export function ReviewPage({ counts }: { counts: Counts }) {
  const store = useAppStore();
  const { repo, base, head, file } = useApp((s) => s.view);
  const repoId = repo === "" ? null : Number(repo);
  const ready = repoId !== null && base !== "" && head !== "";
  const revisions = useMemo<ReviewRevisions | null>(
    () => (ready ? { repo: repoId, base, head } : null),
    [ready, repoId, base, head],
  );
  const files = useLoad(
    revisions === null ? null : `${revisions.repo}|${base}|${head}`,
    () => fetchReviewFiles(revisions as ReviewRevisions),
  );
  const selected: ReviewChangedFile | null =
    files !== null && files.state === "ready" ? (files.data.find((f) => f.path === file) ?? null) : null;
  const diff = useLoad(
    revisions === null || selected === null ? null : `${revisions.repo}|${base}|${head}|${selected.path}`,
    () => fetchReviewFile(revisions as ReviewRevisions, selected as ReviewChangedFile),
  );

  // Reload the personal review when the card's validation changes (a verdict
  // given elsewhere, the PR opening) as well as after an action here.
  const [verdicts, setVerdicts] = useState(0);
  const validation = useApp((s) => s.extras[`${repo}:${head}`]?.agent.validation);
  const personal = useLoad(
    revisions === null ? null : `personal|${repo}|${head}|${verdicts}|${JSON.stringify(validation ?? null)}`,
    () => fetchPersonalReview(Number(repo), head),
  );
  const reload = useCallback(() => setVerdicts((n) => n + 1), []);
  const reviewOf = new Map(
    personal !== null && personal.state === "ready" && personal.data !== null
      ? personal.data.files.map((f) => [f.path, f.state] as const)
      : [],
  );

  const [definition, setDefinition] = useState<{ side: ReviewSide; target: ReviewDefinitionTarget } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const onDefinition = useCallback((side: ReviewSide, target: ReviewDefinitionTarget) => {
    setProblem(null);
    setDefinition({ side, target });
  }, []);

  const open = (path: string): void => {
    setDefinition(null);
    setProblem(null);
    store.getState().setView({ ...store.getState().view, file: path });
  };

  return (
    <>
      <FilterBar counts={counts} />
      {personal !== null && personal.state === "ready" && personal.data !== null && (
        <PersonalReviewBar state={personal.data} selected={file === "" ? null : file} onDone={reload} />
      )}
      {!ready ? (
        <div className="empty">Pick a repo and give a base and a head revision to see what changed between them.</div>
      ) : files === null ? (
        <div className="empty">Loading…</div>
      ) : files.state === "error" ? (
        <div className="empty error">{files.message}</div>
      ) : (
        <div className="review">
          <ul className="review-files" aria-label="Changed files">
            {files.data.map((f) => (
              <li key={f.path}>
                <button type="button" aria-pressed={f.path === file} title={STATUS_TITLE[f.status]} onClick={() => open(f.path)}>
                  <span className={`review-status review-status-${f.status}`}>{f.status}</span>
                  {f.old_path === null ? f.path : `${f.old_path} → ${f.path}`}
                  {reviewOf.get(f.path) ? ` ${FILE_GLYPH[reviewOf.get(f.path) as keyof typeof FILE_GLYPH]}` : ""}
                </button>
              </li>
            ))}
            {files.data.length === 0 ? <li className="empty">No changes.</li> : null}
          </ul>
          <div className="review-body">
            {problem !== null ? <div className="notice">{problem}</div> : null}
            {selected === null ? (
              <div className="empty">Pick a file.</div>
            ) : diff === null ? (
              <div className="empty">{`Loading ${selected.path}…`}</div>
            ) : diff.state === "error" ? (
              <div className="empty error">{diff.message}</div>
            ) : (
              <>
                <div className="review-heading">
                  {`${selected.path}: ${diff.data.difftastic.language}`}
                  {diff.data.difftastic.language === "Text" ? " (line diff: no syntax tree for this language)" : ""}
                </div>
                <DiffPane revisions={revisions as ReviewRevisions} diff={diff.data} onDefinition={onDefinition} onError={setProblem} />
              </>
            )}
            {definition !== null && revisions !== null ? (
              <DefinitionPanel
                revisions={revisions}
                side={definition.side}
                target={definition.target}
                onClose={() => setDefinition(null)}
              />
            ) : null}
          </div>
        </div>
      )}
    </>
  );
}
