import { useEffect, useState } from "react";
import { useCounts, useRows } from "../store/selectors";
import { useApp } from "../store/StoreContext";
import { CardList } from "./CardList";
import { FilterBar } from "./FilterBar";
import { FocusBar } from "./FocusBar";
import { LiveIndicator } from "./LiveIndicator";
import { TableView } from "./TableView";
import { TooltipProvider } from "./Tooltip";
import { UntrackedCallout } from "./UntrackedCallout";
import { NowContext } from "./useNow";

/** How often ages and timeline tracks are re-drawn between server updates. */
const CLOCK_MS = 30_000;

export function App({ clock = Date.now }: { clock?: () => number }) {
  const [now, setNow] = useState(() => clock());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(clock()), CLOCK_MS);
    return () => window.clearInterval(timer);
  }, [clock]);
  return (
    <NowContext.Provider value={now}>
      <TooltipProvider>
        <header>
          <h1>toolbelt factory</h1>
          <LiveIndicator />
        </header>
        <main>
          <Content />
        </main>
      </TooltipProvider>
    </NowContext.Provider>
  );
}

function Content() {
  const ready = useApp((s) => s.ready);
  const error = useApp((s) => s.error);
  const repos = useApp((s) => s.repos);
  const view = useApp((s) => s.view.view);
  const mode = useApp((s) => s.view.mode);
  const rows = useRows();
  const counts = useCounts(rows);

  if (!ready) {
    return error ? (
      <div className="empty error">{`Could not load the stacks: ${error}`}</div>
    ) : (
      <div className="empty">Loading…</div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="empty">
        No tracked stacks. Use <code>tt g append</code> to start one.
      </div>
    );
  }
  return (
    <>
      <FilterBar counts={counts} />
      <div id="notices">
        {repos
          .filter((repo) => repo.path_missing)
          .map((repo) => (
            <div className="notice" key={repo.id}>
              {`${repo.name}: checkout no longer exists at ${repo.path}; PR status unavailable.`}
            </div>
          ))}
      </div>
      <UntrackedCallout />
      {/* The views that draw the stack as a tree can be focused on a branch. */}
      {(view === "stacked" || (view === "table" && mode === "tree")) && <FocusBar rows={rows} />}
      {view === "table" ? <TableView rows={rows} /> : <CardList rows={rows} />}
      {counts.visible === 0 && counts.inRepo > 0 && (
        <div id="no-match" className="empty">
          No branches match the current filters.
        </div>
      )}
    </>
  );
}
