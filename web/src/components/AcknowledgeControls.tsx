// A merged PR that received review comments stays on the page until the user has
// read them and acknowledges: the comments (who, the start of each, a link) and
// the button. The acknowledgement is kept on the server; the card's status
// event drops the card once it is stored.
import { useState } from "react";
import { acknowledgeComments } from "../api/client";
import type { ReviewCommentInfo } from "../api/generated";

const KIND_LABEL: Record<ReviewCommentInfo["kind"], string> = {
  review: "review",
  thread: "inline comment",
  conversation: "comment",
};

export function AcknowledgeButton({ id }: { id: string }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const acknowledge = (): void => {
    setBusy(true);
    setProblem(null);
    acknowledgeComments(id)
      .catch((error: unknown) => {
        setProblem(error instanceof Error ? error.message : String(error));
        setBusy(false);
      });
    // On success the card's status event removes this button; a failure re-enables it.
  };

  return (
    <>
      <button
        type="button"
        className="merge-button acknowledge-button"
        disabled={busy}
        title="Marks the comments on this merged PR as read, which removes the card"
        onClick={acknowledge}
      >
        {busy ? "Acknowledging…" : "Acknowledge"}
      </button>
      {problem && (
        <span className="error" role="alert">
          {` ${problem}`}
        </span>
      )}
    </>
  );
}

export function AcknowledgeComments({ comments }: { comments: ReviewCommentInfo[] }) {
  return (
    <ul className="acknowledge-comments">
      {comments.map((comment) => (
        <li key={comment.url}>
          <a href={comment.url} target="_blank" rel="noreferrer">
            {comment.author}
          </a>
          <span className="muted"> ({KIND_LABEL[comment.kind]})</span>
          <div className="acknowledge-comment-body">{comment.body}</div>
        </li>
      ))}
    </ul>
  );
}
