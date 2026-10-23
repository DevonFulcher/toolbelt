import { useEffect, useState } from "react";
import { refreshBranch } from "../api/client";
import { useApp } from "../store/StoreContext";

/** Longest the button stays on "Refreshing…" when no lookup arrives. */
const WAIT_MS = 30_000;

/** Looks the card's PR up again now instead of waiting for the next poll. It says it
 * is refreshing until the lookup arrives (the card's `updated … ago` restarts, or its
 * error changes), or the request fails. */
export function RefreshButton({ id }: { id: string }) {
  const fetchedAt = useApp((s) => s.fetchedAt[id]);
  const fetchError = useApp((s) => s.fetchError[id]);
  // What the card showed when the button was pressed: refreshing until it changes.
  const [before, setBefore] = useState<{ fetchedAt: number | undefined; error: string | undefined } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (before === null) return;
    if (fetchedAt !== before.fetchedAt || fetchError !== before.error) {
      setBefore(null);
      return;
    }
    const timer = window.setTimeout(() => setBefore(null), WAIT_MS);
    return () => window.clearTimeout(timer);
  }, [before, fetchedAt, fetchError]);

  const press = (): void => {
    setProblem(null);
    setBefore({ fetchedAt, error: fetchError });
    refreshBranch(id).catch((error: unknown) => {
      setBefore(null);
      setProblem(error instanceof Error ? error.message : String(error));
    });
  };

  return (
    <>
      <button
        type="button"
        className="agents-toggle refresh-pr"
        title="Look this PR up again now instead of waiting for the next poll"
        disabled={before !== null}
        onClick={press}
      >
        {before !== null ? "Refreshing…" : "Refresh"}
      </button>
      {problem && (
        <span className="error" role="alert">
          {` ${problem}`}
        </span>
      )}
    </>
  );
}
