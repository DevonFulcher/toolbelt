// The view state (view, filters, sort) and its URL query string. Same
// parameters as the previous page: `view=table|stacked|grouped|aggregate|spend`
// (`weeks=4|26|52` for the aggregate's window; 12 is the default, and
// `weekends=exclude` for its lead times; `days=1|30` for the spend view's tables, 7 is the default), `mode=flat`,
// `compact=1`, `done=1`, `sort`/`dir`, `q`, `repo`, `root` (the branch id the
// stacked view and the table's tree are focused on) and the per-column
// filters. Only non-default values are written, so the default view is a
// bare URL.
import type { FilterKey } from "../api/generated";

export const VIEWS = ["priorities", "table", "stacked", "grouped", "aggregate", "spend", "agents", "proposals", "review"] as const;
export type View = (typeof VIEWS)[number];

// The aggregate view's time windows, in weeks (the server's `WINDOWS`).
export const WINDOWS = [4, 12, 26, 52] as const;
export type Weeks = (typeof WINDOWS)[number];

// The spend view's breakdown windows, in days (the server's `BREAKDOWN_DAYS`).
export const SPEND_DAYS = [1, 7, 30] as const;
export type SpendDays = (typeof SPEND_DAYS)[number];

// How the table view lays its rows out.
export const MODES = ["tree", "flat"] as const;
export type Mode = (typeof MODES)[number];

export const COLUMNS = [
  "repo",
  "branch",
  "next",
  "pr",
  "ci",
  "review",
  "comments",
  "merge",
  "task",
  "session",
] as const;
export type Column = (typeof COLUMNS)[number];

// Columns with an exact-match value filter (the repo filter is separate: it
// scopes the whole view rather than hiding rows).
export const VALUE_FILTERS: readonly FilterKey[] = [
  "next",
  "pr",
  "ci",
  "review",
  "comments",
  "merge",
];

export interface ViewState {
  view: View;
  /** Table view only: "tree" (stacks indented) or "flat". */
  mode: Mode;
  sort: Column | "";
  dir: "asc" | "desc";
  q: string;
  repo: string;
  next: string;
  pr: string;
  ci: string;
  review: string;
  comments: string;
  merge: string;
  /** "1" includes merged/closed branches, which are hidden by default. */
  done: "" | "1";
  /** Aggregate view only: how many weeks back it looks. */
  weeks: Weeks;
  /** Spend view only: how many days its by-model and by-task tables cover. */
  days: SpendDays;
  /** Aggregate view only: "exclude" drops Saturdays and Sundays from lead times. */
  weekends: "" | "exclude";
  /** "1" collapses the PR/CI/Review/Comments/Merge columns into one. */
  compact: "" | "1";
  /**
   * The branch id the stacked view and the table's tree are focused on (they
   * show only its subtree); "" shows every stack from its root.
   */
  root: string;
  /** Review view only: the revisions `base...head` it shows the changes of, and the open file. */
  base: string;
  head: string;
  file: string;
}

export const DEFAULT_STATE: ViewState = {
  // Sections by what happens next: what is waiting on whom.
  view: "grouped",
  mode: "tree",
  sort: "",
  dir: "asc",
  q: "",
  repo: "",
  next: "",
  pr: "",
  ci: "",
  review: "",
  comments: "",
  merge: "",
  done: "",
  weeks: 12,
  days: 7,
  weekends: "",
  compact: "",
  root: "",
  base: "",
  head: "",
  file: "",
};

const isOneOf = <T extends string>(list: readonly T[], value: string | null): value is T =>
  value !== null && (list as readonly string[]).includes(value);

/**
 * Parses a query string. Unknown or retired keys (`view=timeline`, `kind`,
 * `order`, `group`) are ignored, so an old link just opens the default.
 * Legacy views: `view=cards` is the grouped view, `view=tree|flat` the table
 * (flat layout for `flat`). The table-only `mode` and `compact` parameters
 * without a view also mean the table: before grouped became the default, a
 * bare URL was the table.
 */
export function parseState(search: string): ViewState {
  const params = new URLSearchParams(search);
  const state: ViewState = { ...DEFAULT_STATE };
  const view = params.get("view");
  const mode = params.get("mode");
  if (isOneOf(VIEWS, view)) state.view = view;
  else if (view === "cards") state.view = "grouped";
  else if (view === "tree" || view === "flat") {
    state.view = "table";
    state.mode = view;
  } else if (view === null && (isOneOf(MODES, mode) || params.get("compact") === "1")) {
    state.view = "table";
  }
  if (isOneOf(MODES, mode)) state.mode = mode;
  const sort = params.get("sort");
  if (isOneOf(COLUMNS, sort)) state.sort = sort;
  if (params.get("dir") === "desc") state.dir = "desc";
  state.q = params.get("q") ?? "";
  state.repo = params.get("repo") ?? "";
  for (const key of VALUE_FILTERS) state[key] = params.get(key) ?? "";
  if (params.get("done") === "1") state.done = "1";
  if (params.get("compact") === "1") state.compact = "1";
  const weeks = Number(params.get("weeks"));
  if ((WINDOWS as readonly number[]).includes(weeks)) state.weeks = weeks as Weeks;
  const days = Number(params.get("days"));
  if ((SPEND_DAYS as readonly number[]).includes(days)) state.days = days as SpendDays;
  if (params.get("weekends") === "exclude") state.weekends = "exclude";
  state.root = params.get("root") ?? "";
  state.base = params.get("base") ?? "";
  state.head = params.get("head") ?? "";
  state.file = params.get("file") ?? "";
  return state;
}

/**
 * The state a page load starts with: the URL's, except that the remembered
 * compact choice applies when the URL does not say.
 */
export function initialState(search: string, storedCompact: string | null): ViewState {
  const state = parseState(search);
  if (!new URLSearchParams(search).has("compact") && storedCompact === "1") {
    state.compact = "1";
  }
  return state;
}

export function serializeState(state: ViewState): string {
  const params = new URLSearchParams();
  if (state.view !== DEFAULT_STATE.view) params.set("view", state.view);
  if (state.view === "table" && state.mode !== DEFAULT_STATE.mode) {
    params.set("mode", state.mode);
  }
  if (state.sort) {
    params.set("sort", state.sort);
    if (state.dir !== DEFAULT_STATE.dir) params.set("dir", state.dir);
  }
  if (state.q) params.set("q", state.q);
  if (state.repo) params.set("repo", state.repo);
  for (const key of VALUE_FILTERS) {
    if (state[key]) params.set(key, state[key]);
  }
  if (state.done) params.set("done", "1");
  // The aggregate view's own window.
  if (state.view === "aggregate" && state.weeks !== DEFAULT_STATE.weeks) {
    params.set("weeks", String(state.weeks));
  }
  if (state.view === "aggregate" && state.weekends) params.set("weekends", state.weekends);
  // The spend view's own window.
  if (state.view === "spend" && state.days !== DEFAULT_STATE.days) {
    params.set("days", String(state.days));
  }
  // Table-only, like `mode`: a card view's URL does not carry it.
  if (state.view === "table" && state.compact) params.set("compact", "1");
  // The views that draw a tree; the grouped view and the flat table ignore it.
  if (state.root && (state.view === "stacked" || (state.view === "table" && state.mode === "tree"))) {
    params.set("root", state.root);
  }
  if (state.view === "review") {
    if (state.base) params.set("base", state.base);
    if (state.head) params.set("head", state.head);
    if (state.file) params.set("file", state.file);
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** Clicking the sorted column flips its direction; a new column starts asc. */
export function toggleSort(state: ViewState, column: Column): ViewState {
  if (state.sort === column) {
    return { ...state, dir: state.dir === "asc" ? "desc" : "asc" };
  }
  return { ...state, sort: column, dir: "asc" };
}
