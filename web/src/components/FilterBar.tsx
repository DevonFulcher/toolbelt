import type { FilterKey } from "../api/generated";
import {
  COLUMNS,
  DEFAULT_STATE,
  WINDOWS,
  type Column,
  type View,
  type ViewState,
} from "../logic/viewState";
import type { Counts } from "../store/selectors";
import { useApp, useAppStore } from "../store/StoreContext";

// The default view first.
const ORDER: View[] = ["grouped", "stacked", "table", "aggregate"];

const VIEW_LABELS: Record<View, { label: string; title: string }> = {
  grouped: { label: "Grouped", title: "Cards grouped by what happens next" },
  stacked: { label: "Stacked", title: "Cards by repo and stack, with the stack drawn" },
  table: { label: "Table", title: "One row per branch" },
  aggregate: {
    label: "Aggregate",
    title: "How work is flowing: throughput, lead time and where tasks wait, over time",
  },
};

const COLUMN_LABELS: Record<Column, string> = {
  repo: "Repo",
  branch: "Branch",
  next: "Next",
  pr: "PR",
  ci: "CI",
  review: "Review",
  comments: "Comments",
  merge: "Merge",
  task: "Task",
  session: "Session",
};

/** The view toggle, filters, sort, include-done / compact / clear, and the count. */
export function FilterBar({ counts }: { counts: Counts }) {
  const store = useAppStore();
  const view = useApp((s) => s.view);
  const repos = useApp((s) => s.repos);
  const filters = useApp((s) => s.filters);
  const set = (patch: Partial<ViewState>): void => store.getState().setView({ ...view, ...patch });

  return (
    <form id="controls" onSubmit={(event) => event.preventDefault()}>
      {view.view !== "aggregate" && (
        <label>
          Filter
          <input
            type="search"
            name="q"
            placeholder="branch, PR #, task…"
            autoComplete="off"
            value={view.q}
            onChange={(event) => set({ q: event.target.value })}
          />
        </label>
      )}
      <label>
        Repo
        <select name="repo" value={view.repo} onChange={(event) => set({ repo: event.target.value })}>
          <option value="">All</option>
          {repos.map((repo) => (
            <option key={repo.id} value={String(repo.id)}>
              {repo.name}
            </option>
          ))}
        </select>
      </label>
      {view.view !== "aggregate" && (
        <>
          {filters.map((filter) => (
            <label key={filter.key}>
              {filter.label}
              <select
                name={filter.key}
                value={view[filter.key as FilterKey]}
                onChange={(event) => set({ [filter.key]: event.target.value })}
              >
                <option value="">Any</option>
                {filter.options.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </>
      )}
      <span className="seg" role="group" aria-label="View">
        {ORDER.map((name) => (
          <button
            key={name}
            type="button"
            data-view={name}
            aria-pressed={view.view === name}
            title={VIEW_LABELS[name].title}
            onClick={() => set({ view: name })}
          >
            {VIEW_LABELS[name].label}
          </button>
        ))}
      </span>
      {view.view === "aggregate" && (
        <span className="seg" role="group" aria-label="Window">
          {WINDOWS.map((weeks) => (
            <button
              key={weeks}
              type="button"
              data-weeks={weeks}
              aria-pressed={view.weeks === weeks}
              title={`The last ${weeks} weeks`}
              onClick={() => set({ weeks })}
            >
              {`${weeks} weeks`}
            </button>
          ))}
        </span>
      )}
      {view.view === "table" && (
        <span className="seg" role="group" aria-label="Table layout">
          {(["tree", "flat"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              data-mode={mode}
              aria-pressed={view.mode === mode}
              onClick={() => set({ mode })}
            >
              {mode === "tree" ? "Tree" : "Flat"}
            </button>
          ))}
        </span>
      )}
      {view.view !== "table" && view.view !== "aggregate" && (
        <label>
          Sort
          <select
            name="sort"
            value={view.sort}
            onChange={(event) => set({ sort: event.target.value as Column | "", dir: "asc" })}
          >
            <option value="">Stack order</option>
            {COLUMNS.map((column) => (
              <option key={column} value={column}>
                {COLUMN_LABELS[column]}
              </option>
            ))}
          </select>
        </label>
      )}
      {view.view !== "aggregate" && (
        <>
          <button
            type="button"
            id="done"
            aria-pressed={view.done === "1"}
            title="Show merged and closed branches"
            onClick={() => set({ done: view.done ? "" : "1" })}
          >
            Include done
          </button>
          {view.view === "table" && (
            <button
              type="button"
              id="compact"
              aria-pressed={view.compact === "1"}
              title="Combine the PR, CI, review, comments and merge columns"
              onClick={() => set({ compact: view.compact ? "" : "1" })}
            >
              Compact
            </button>
          )}
          <button
            type="button"
            id="clear"
            onClick={() =>
              store.getState().setView({
                ...DEFAULT_STATE,
                view: view.view,
                mode: view.mode,
                compact: view.compact,
                root: view.root,
              })
            }
          >
            Clear
          </button>
          <span id="count" aria-live="polite">
            {`${counts.visible} of ${counts.inRepo} branches`}
            {counts.doneHidden > 0 ? ` (${counts.doneHidden} done hidden)` : ""}
          </span>
        </>
      )}
    </form>
  );
}
