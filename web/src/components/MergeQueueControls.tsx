// A PR waiting on CI can be queued to merge as soon as the CI passes; this is its
// card's control for that: the button to queue it, what a queued one says (with a
// way to cancel), and why one was taken off the queue without merging.
import { useState } from "react";
import { queueMerge, unqueueMerge } from "../api/client";
import type { MergeQueueInfo } from "../api/generated";

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
          <span className="pill queue-pill" title="Merges on its own once its CI has passed">
            Queued: merges when CI passes
          </span>{" "}
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
          title="Merges this PR on its own once its CI has passed"
          onClick={() => act(() => queueMerge(id))}
        >
          Merge when CI passes
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
