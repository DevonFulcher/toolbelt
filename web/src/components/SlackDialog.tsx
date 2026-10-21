// Post (or bump) a PR in Slack: the message toolbelt would send, for the user to
// read, edit and confirm. Opened by the Post in Slack / Bump in Slack button of a
// card; nothing is sent until Send is pressed.
import { useEffect, useState } from "react";
import { fetchSlackDraft, sendSlack } from "../api/client";
import type { SlackDraft, SlackSent } from "../api/generated";
import { Dialog } from "./Dialog";

type Phase =
  | { name: "loading" }
  | { name: "failed"; text: string }
  | { name: "ready"; draft: SlackDraft }
  | { name: "sending"; draft: SlackDraft }
  | { name: "sent"; draft: SlackDraft; result: SlackSent };

export function SlackDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const [phase, setPhase] = useState<Phase>({ name: "loading" });
  const [text, setText] = useState("");
  const [assigneeId, setAssigneeId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    fetchSlackDraft(id)
      .then((draft) => {
        if (!current) return;
        setText(draft.text);
        setAssigneeId(draft.assignee?.id ?? null);
        setPhase({ name: "ready", draft });
      })
      .catch((error: unknown) => {
        if (current) setPhase({ name: "failed", text: error instanceof Error ? error.message : String(error) });
      });
    return () => {
      current = false;
    };
  }, [id]);

  const draft = phase.name === "ready" || phase.name === "sending" || phase.name === "sent" ? phase.draft : null;
  const title = draft ? (draft.kind === "post" ? "Post in Slack" : "Bump in Slack") : "Slack";

  // Asking someone else changes the name in the text, keeping any edits made to it.
  const ask = (nextId: string): void => {
    if (!draft) return;
    const before = draft.candidates.find((person) => person.id === assigneeId);
    const after = draft.candidates.find((person) => person.id === nextId);
    if (before && after) setText((current) => current.split(`@${before.name}`).join(`@${after.name}`));
    setAssigneeId(nextId);
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
      assignee_id: assigneeId,
      thread_ts: ready.thread_ts,
      prs: ready.prs.map((pr) => ({ repo: pr.repo, number: pr.number })),
    })
      .then((result) => setPhase({ name: "sent", draft: ready, result }))
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
          </p>
          <label className="slack-field">
            <span className="slack-label">Ask</span>
            <select
              value={assigneeId ?? ""}
              disabled={phase.name !== "ready"}
              onChange={(event) => ask(event.target.value)}
            >
              {assigneeId === null && <option value="">nobody chosen</option>}
              {draft.candidates.map((person) => (
                <option key={person.id} value={person.id}>
                  {person.name}
                </option>
              ))}
            </select>
          </label>
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
          {phase.name === "sent" ? (
            <p>
              <a href={phase.result.link} target="_blank" rel="noreferrer">
                Posted in Slack
              </a>
              {` (cost $${phase.result.cost_usd.toFixed(2)}). `}
              {phase.result.warning && (
                <span className="error" role="alert">
                  {phase.result.warning}
                </span>
              )}{" "}
              <button type="button" className="agents-toggle" onClick={onClose}>
                Done
              </button>
            </p>
          ) : (
            <p>
              <button
                type="button"
                className="merge-button"
                disabled={phase.name !== "ready" || text.trim() === ""}
                onClick={send}
              >
                {phase.name === "sending" ? "Sending…" : draft.kind === "post" ? "Post" : "Send bump"}
              </button>{" "}
              <span className="muted">Sends exactly this text, with @names turned into mentions.</span>
            </p>
          )}
        </div>
      )}
    </Dialog>
  );
}
