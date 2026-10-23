import { useEffect, useState } from "react";
import { refreshAll } from "../api/client";
import { useApp } from "../store/StoreContext";

/** Longest the button stays on "Refreshing…" when no lookup arrives. */
const WAIT_MS = 30_000;

/** Looks every PR up again now instead of waiting for the next poll. It says it is
 * refreshing until the server's next lookups arrive (the page's `updatedAt`
 * moves), or the request fails. */
export function RefreshButton() {
  const updatedAt = useApp((s) => s.updatedAt);
  // `updatedAt` when the button was pressed: refreshing until it changes.
  const [since, setSince] = useState<{ at: number | null } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (since === null) return;
    if (updatedAt !== since.at) {
      setSince(null);
      return;
    }
    const timer = window.setTimeout(() => setSince(null), WAIT_MS);
    return () => window.clearTimeout(timer);
  }, [since, updatedAt]);

  const press = (): void => {
    setProblem(null);
    setSince({ at: updatedAt });
    refreshAll().catch((error: unknown) => {
      setSince(null);
      setProblem(error instanceof Error ? error.message : String(error));
    });
  };

  return (
    <span id="refresh">
      <button
        type="button"
        className="agents-toggle"
        title="Look every PR up again now instead of waiting for the next poll"
        disabled={since !== null}
        onClick={press}
      >
        {since !== null ? "Refreshing…" : "Refresh PRs"}
      </button>
      {problem && (
        <span className="error" role="alert">{` ${problem}`}</span>
      )}
    </span>
  );
}
