// Post (or bump) a PR in Slack: the message toolbelt would send, for the user to
// read, edit and confirm. Opened by the Post in Slack / Bump in Slack button of a
// card; nothing is sent until Send is pressed.
import { useEffect, useState } from "react";
import { fetchSlackDraft, sendSlack } from "../api/client";
import type { SlackDraft } from "../api/generated";
import { renameMention } from "../logic/slackText";
import { Dialog } from "./Dialog";

type Phase =
  | { name: "loading" }
  | { name: "failed"; text: string }
  | { name: "ready"; draft: SlackDraft }
  | { name: "sending"; draft: SlackDraft };

export function SlackDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const [phase, setPhase] = useState<Phase>({ name: "loading" });
  const [text, setText] = useState("");
  // Who is asked about each PR of the draft, in its order (a bump asks one person about all).
  const [assignees, setAssignees] = useState<(string | null)[]>([]);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    fetchSlackDraft(id)
      .then((draft) => {
        if (!current) return;
        setText(draft.text);
        setAssignees(draft.prs.map((pr) => pr.assignee?.id ?? draft.assignee?.id ?? null));
        setPhase({ name: "ready", draft });
      })
      .catch((error: unknown) => {
        if (current) setPhase({ name: "failed", text: error instanceof Error ? error.message : String(error) });
      });
    return () => {
      current = false;
    };
  }, [id]);

  const draft = phase.name === "ready" || phase.name === "sending" ? phase.draft : null;
  const title = draft ? (draft.kind === "post" ? "Post in Slack" : "Bump in Slack") : "Slack";

  const nameOf = (personId: string | null): string | undefined =>
    draft?.candidates.find((person) => person.id === personId)?.name;
  const several = draft !== null && draft.kind === "post" && draft.prs.length > 1;

  // Asking someone else changes the name in the text, keeping any edits made to it.
  // With several PRs, asking about one changes only the lines linking that PR.
  const askAbout = (index: number, nextId: string): void => {
    if (!draft) return;
    const before = nameOf(assignees[index]);
    const after = nameOf(nextId);
    if (before && after) setText((current) => renameMention(current, before, after, draft.prs[index].number));
    setAssignees((current) => current.map((id, i) => (i === index ? nextId : id)));
  };

  // Asking everyone (or the one person of a single PR or a bump) changes every line.
  const askAll = (nextId: string): void => {
    if (!draft) return;
    const after = nameOf(nextId);
    if (after) {
      setText((current) =>
        several
          ? draft.prs.reduce<string>((acc, pr, index) => {
              const before = nameOf(assignees[index]);
              return before ? renameMention(acc, before, after, pr.number) : acc;
            }, current)
          : assignees.reduce<string>((acc, id) => {
              const before = nameOf(id);
              return before ? renameMention(acc, before, after) : acc;
            }, current),
      );
    }
    setAssignees((current) => current.map(() => nextId));
  };

  const send = (): void => {
    if (phase.name !== "ready") return;
    const { draft: ready } = phase;
    setProblem(null);
    setPhase({ name: "sending", draft: ready });
    sendSlack({
      id,
      kind: ready.kind,
      text,
      assignee_id: assignees[0] ?? null,
      thread_ts: ready.thread_ts,
      prs: ready.prs.map((pr, index) => ({ repo: pr.repo, number: pr.number, assignee_id: assignees[index] ?? null })),
    })
      .then(onClose) // it posts in the background: the card shows how it went
      .catch((error: unknown) => {
        setProblem(error instanceof Error ? error.message : String(error));
        setPhase({ name: "ready", draft: ready });
      });
  };

  return (
    <Dialog title={title} onClose={onClose}>
      {phase.name === "loading" && <p className="muted">Writing the message…</p>}
      {phase.name === "failed" && (
        <p className="error" role="alert">
          {phase.text}
        </p>
      )}
      {draft && (
        <div className="slack-dialog">
          <p className="muted">
            {draft.kind === "post" ? "Posts to" : "Replies in a thread of"} <code>{draft.channel}</code> about{" "}
            {draft.prs.map((pr, index) => (
              <span key={pr.url}>
                {index > 0 && ", "}
                <a href={pr.url} target="_blank" rel="noreferrer">{`${pr.repo}#${pr.number}`}</a>
              </span>
            ))}
            .
            {draft.thread_link && (
              <>
                {" "}
                <a href={draft.thread_link} target="_blank" rel="noreferrer">
                  Original post in Slack
                </a>
              </>
            )}
          </p>
          <label className="slack-field">
            <span className="slack-label">{several ? "Ask everyone" : "Ask"}</span>
            <select
              value={assignees.every((id) => id === assignees[0]) ? (assignees[0] ?? "") : ""}
              disabled={phase.name !== "ready"}
              onChange={(event) => askAll(event.target.value)}
            >
              {(assignees[0] === null || !assignees.every((id) => id === assignees[0])) && (
                <option value="" disabled>
                  {several && assignees.some((id) => id !== null) ? "different people" : "nobody chosen"}
                </option>
              )}
              {draft.candidates.map((person) => (
                <option key={person.id} value={person.id}>
                  {person.name}
                </option>
              ))}
            </select>
          </label>
          {several && (
            <ul className="slack-per-pr" aria-label="Who is asked about each PR">
              {draft.prs.map((pr, index) => (
                <li key={pr.url}>
                  <label>
                    <span className="slack-pr-name">{`${pr.repo}#${pr.number}`}</span>{" "}
                    <span className="muted">{pr.title}</span>{" "}
                    <select
                      aria-label={`Ask about ${pr.repo}#${pr.number}`}
                      value={assignees[index] ?? ""}
                      disabled={phase.name !== "ready"}
                      onChange={(event) => askAbout(index, event.target.value)}
                    >
                      {assignees[index] === null && <option value="">nobody chosen</option>}
                      {draft.candidates.map((person) => (
                        <option key={person.id} value={person.id}>
                          {person.name}
                        </option>
                      ))}
                    </select>
                  </label>
                </li>
              ))}
            </ul>
          )}
          <label className="slack-field">
            <span className="slack-label">Message</span>
            <textarea
              value={text}
              disabled={phase.name !== "ready"}
              onChange={(event) => setText(event.target.value)}
              rows={Math.min(Math.max(text.split("\n").length + 1, 6), 18)}
            />
          </label>
          {problem && (
            <p className="error" role="alert">
              {problem}
            </p>
          )}
          <p>
            <button
              type="button"
              className="merge-button"
              disabled={phase.name !== "ready" || text.trim() === ""}
              onClick={send}
            >
              {phase.name === "sending" ? "Sending…" : draft.kind === "post" ? "Post" : "Send bump"}
            </button>{" "}
            <span className="muted">
              Sends exactly this text, with @names turned into mentions. It posts in the background: the card shows when
              it is done.
            </span>
          </p>
        </div>
      )}
    </Dialog>
  );
}
