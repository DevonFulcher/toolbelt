import { useEffect, useMemo } from "react";
import { branchCountText, breadcrumbs, focusCounts, knownRoot, upTarget } from "../logic/focus";
import { matches } from "../logic/layout";
import type { Row } from "../logic/rows";
import { serializeState } from "../logic/viewState";
import { useApp, useAppStore } from "../store/StoreContext";

/**
 * Shown while the stacked view or the table's tree is focused on a branch:
 * the way back up (breadcrumbs, "Up one level", "Show whole stack") and what
 * is in focus. Sticky, so it stays above the stack while it scrolls.
 */
export function FocusBar({ rows }: { rows: Row[] }) {
  const store = useAppStore();
  const view = useApp((s) => s.view);
  const root = knownRoot(rows, view.root);
  const crumbs = useMemo(() => (root ? breadcrumbs(rows, root) : null), [rows, root]);

  // A new root starts at the top of the page.
  useEffect(() => {
    if (root && typeof window.scrollTo === "function") window.scrollTo({ top: 0 });
  }, [root]);

  if (!root || !crumbs) return null;
  const up = upTarget(rows, root);
  const counts = focusCounts(rows, root, (row) => matches(row, view));
  const hrefTo = (id: string): string => serializeState({ ...view, root: id });
  const go = (id: string) => (event: { preventDefault(): void }) => {
    event.preventDefault();
    store.getState().focusRoot(id);
  };

  return (
    <nav className="focus-bar" aria-label="Focused stack">
      <ol className="focus-crumbs">
        <li className="focus-repo">{crumbs.repoName}</li>
        {crumbs.segments.map((segment) =>
          segment.kind === "branch" ? (
            <li key={segment.crumb.id}>
              <a href={hrefTo(segment.crumb.id)} onClick={go(segment.crumb.id)}>
                {segment.crumb.name}
              </a>
            </li>
          ) : (
            <li key="gap" className="focus-gap">
              <details>
                <summary title={segment.hidden.map((crumb) => crumb.name).join(" › ")}>…</summary>
                <ul>
                  {segment.hidden.map((crumb) => (
                    <li key={crumb.id}>
                      <a href={hrefTo(crumb.id)} onClick={go(crumb.id)}>
                        {crumb.name}
                      </a>
                    </li>
                  ))}
                </ul>
              </details>
            </li>
          ),
        )}
        <li className="focus-current" aria-current="page">
          {crumbs.current.name}
        </li>
      </ol>
      <span className="focus-size">
        {`Focused on ${crumbs.current.name} (${branchCountText(counts)})`}
      </span>
      <span className="focus-actions">
        <button type="button" onClick={go(up)}>
          ↑ Up one level
        </button>
        <button type="button" onClick={go("")}>
          Show whole stack
        </button>
      </span>
    </nav>
  );
}
