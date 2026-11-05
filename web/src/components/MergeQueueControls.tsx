// A PR waiting on CI, or blocked on its parent PR, can be queued to merge as soon
// as it is ready (CI passed, approved, and any parent merged); this is its card's
// control for that: the button to queue it, what a queued one says (with a way to
// cancel), and why one was taken off the queue without merging.
import { useState } from "react";
import { queueMerge, unqueueMerge } from "../api/client";
import type { MergeQueueInfo } from "../api/generated";

/** "8:00 AM", with the weekday when it is not today. */
function resumeTime(iso: string): string {
  const at = new Date(iso);
  const time = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return at.toDateString() === new Date().toDateString()
    ? time
    : `${at.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

export function MergeQueueControls({ id, queue }: { id: string; queue: MergeQueueInfo }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const act = (request: () => Promise<unknown>): void => {
    setBusy(true);
    setProblem(null);
    request()
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : String(error)))
      .finally(() => setBusy(false));
  };

  return (
    <>
      {queue.queued && (
        <>
          <span className="pill queue-pill" title="Merges on its own once it is ready: CI passed, approved, and any PR it is stacked on merged">
            Queued: merges when ready
          </span>{" "}
          {queue.paused_until && (
            <>
              <span className="pill" title="The queue starts no merges in the evening; the Merge button still works">
                Paused until {resumeTime(queue.paused_until)}
              </span>{" "}
            </>
          )}
          <button type="button" className="agents-toggle" disabled={busy} onClick={() => act(() => unqueueMerge(id))}>
            Cancel
          </button>
        </>
      )}
      {queue.can_queue && (
        <button
          type="button"
          className="merge-button"
          disabled={busy}
          title="Merges this PR on its own once it is ready: CI passed, approved, and any PR it is stacked on merged"
          onClick={() => act(() => queueMerge(id))}
        >
          Merge when ready
        </button>
      )}
      {(queue.notice ?? problem) && (
        <span className="error" role="alert">
          {` ${queue.notice ?? problem}`}
        </span>
      )}
    </>
  );
}
