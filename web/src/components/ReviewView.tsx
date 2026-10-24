// The review view: the files changed between two revisions of a repo, each as
// difftastic's structural diff in a read-only merge view. Hover shows what a
// language server says about the code under the pointer; Cmd/Ctrl-click opens
// its definition below. Everything is read from git when asked for: nothing
// here is pushed by the server.
import { MergeView } from "@codemirror/merge";
import { EditorView } from "@codemirror/view";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchReviewFile,
  fetchReviewFiles,
  fetchReviewText,
  type ReviewRevisions,
  type ReviewSide,
} from "../api/client";
import type { ReviewChangedFile, ReviewDefinitionTarget, ReviewFile } from "../api/generated";
import { buildChanges } from "../logic/diffChanges";
import type { Counts } from "../store/selectors";
import { useApp, useAppStore } from "../store/StoreContext";
import { FilterBar } from "./FilterBar";
import { isPython, lspExtensions, readOnlyExtensions } from "./reviewEditor";
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
      isPython(path) ? lspExtensions({ revisions, side, path, onDefinition: (t) => onDefinition(side, t), onError }) : [],
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
