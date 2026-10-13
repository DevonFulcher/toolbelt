// The client's one store (Zustand): the snapshot, the live patches applied to
// it, the view state (filters, sort, view, mode) and per-card UI state.
//
// The live stream writes to it from outside React (`live/stream.ts`); the URL
// is kept in step with the view state by `syncUrl`. Components subscribe with
// per-branch selectors (`state.statuses[id]`), and the snapshot is applied
// with structural sharing (an unchanged branch keeps its object), so a status
// patch re-renders only that branch's card.
import { createStore, type StoreApi } from "zustand/vanilla";
import type {
  Branch,
  Catalog,
  FilterSpec,
  LiveStatus,
  Repo,
  Snapshot,
  TaskLive,
  UntrackedPrs,
} from "../api/generated";
import type { ConnectionState } from "../live/connection";
import { DEFAULT_STATE, type View, type ViewState } from "../logic/viewState";

/** A branch without its live parts (those are patched separately). */
export type StaticBranch = Omit<Branch, "status" | "task_live">;

/** Per-card timeline UI state, kept across live updates. */
export interface CardUi {
  hideCommits?: boolean;
  legend?: boolean;
}

/** Remembered between visits (a convenience only: the URL is the state). */
export interface Preferences {
  loadCompact(): string | null;
  saveCompact(value: "" | "1"): void;
  loadCollapsed(): string[];
  saveCollapsed(keys: string[]): void;
  /** The ids of the cards whose chronological log is open. */
  loadLogOpen(): string[];
  saveLogOpen(ids: string[]): void;
  /** The ids of the stacked cards whose descendants are collapsed. */
  loadFolded(): string[];
  saveFolded(ids: string[]): void;
}

export interface AppState {
  /** A snapshot has been loaded. */
  ready: boolean;
  /** The snapshot could not be fetched (and no earlier one is shown). */
  error: string | null;
  repos: Repo[];
  reposById: Record<number, Repo>;
  /** In the server's order (repos by name, each repo depth-first). */
  branches: StaticBranch[];
  branchesById: Record<string, StaticBranch>;
  statuses: Record<string, LiveStatus>;
  /** Only for branches whose task the configured tracker owns. */
  tasks: Record<string, TaskLive>;
  catalog: Catalog | null;
  filters: FilterSpec[];
  /** Open PRs of the user's that are not tracked branches (links only). */
  untracked: UntrackedPrs;
  /** The latest event version seen (the snapshot's, then each event's). */
  version: number | null;
  /** `Date.now()` of the last refresh we know about. */
  updatedAt: number | null;
  connection: ConnectionState;

  view: ViewState;
  cardUi: Record<string, CardUi>;
  /** Keys of the collapsed card sections. */
  collapsed: string[];
  /** Ids of the cards whose chronological log is open (remembered). */
  logOpen: string[];
  /** Stacked view: cards whose descendants are hidden. */
  folded: string[];

  // -- actions --
  applySnapshot(snapshot: Snapshot, now?: number): void;
  setSnapshotError(message: string): void;
  statusChanged(branchId: string, status: LiveStatus, v: number): void;
  taskChanged(branchId: string, task: TaskLive, v: number): void;
  refreshed(v: number): void;
  untrackedChanged(untracked: UntrackedPrs, v: number): void;
  hello(v: number, ageSeconds: number | null): void;
  setConnection(state: ConnectionState): void;
  /** Replaces the view state (the URL follows). */
  setView(view: ViewState): void;
  /**
   * Focuses the stacked view and the table's tree on a branch ("" shows the
   * whole stack), optionally switching to another view at the same time.
   */
  focusRoot(root: string, view?: View): void;
  patchCardUi(id: string, patch: CardUi): void;
  toggleLog(id: string): void;
  toggleFold(id: string): void;
  toggleSection(key: string): void;
  expandSection(key: string): void;
}

const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

/** `next` with every value that equals the one in `prev` replaced by it. */
function share<T>(prev: Record<string, T>, next: Record<string, T>): Record<string, T> {
  const out: Record<string, T> = {};
  for (const [key, value] of Object.entries(next)) {
    out[key] = key in prev && same(prev[key], value) ? prev[key] : value;
  }
  return out;
}

export const noPreferences: Preferences = {
  loadCompact: () => null,
  saveCompact: () => {},
  loadCollapsed: () => [],
  saveCollapsed: () => {},
  loadLogOpen: () => [],
  saveLogOpen: () => {},
  loadFolded: () => [],
  saveFolded: () => {},
};

export interface StoreInit {
  view?: ViewState;
  preferences?: Preferences;
  clock?: () => number;
}

export type AppStore = StoreApi<AppState>;

export function createAppStore(init: StoreInit = {}): AppStore {
  const preferences = init.preferences ?? noPreferences;
  const clock = init.clock ?? Date.now;
  return createStore<AppState>((set, get) => ({
    ready: false,
    error: null,
    repos: [],
    reposById: {},
    branches: [],
    branchesById: {},
    statuses: {},
    tasks: {},
    catalog: null,
    filters: [],
    untracked: { state: "loading" },
    version: null,
    updatedAt: null,
    connection: "connecting",
    view: init.view ?? DEFAULT_STATE,
    cardUi: {},
    collapsed: preferences.loadCollapsed(),
    logOpen: preferences.loadLogOpen(),
    folded: preferences.loadFolded(),

    applySnapshot(snapshot, now = clock()) {
      const prev = get();
      const statuses: Record<string, LiveStatus> = {};
      const tasks: Record<string, TaskLive> = {};
      const statics: StaticBranch[] = [];
      for (const { status, task_live, ...rest } of snapshot.branches) {
        statuses[rest.id] = status;
        if (task_live) tasks[rest.id] = task_live;
        const before = prev.branchesById[rest.id];
        statics.push(before && same(before, rest) ? before : rest);
      }
      const branchesById = Object.fromEntries(statics.map((b) => [b.id, b]));
      // A focus root that is not (or no longer) a branch is dropped.
      const view =
        prev.view.root && !(prev.view.root in branchesById) ? { ...prev.view, root: "" } : prev.view;
      set({
        ready: true,
        error: null,
        view,
        repos: same(prev.repos, snapshot.repos) ? prev.repos : snapshot.repos,
        reposById: Object.fromEntries(snapshot.repos.map((repo) => [repo.id, repo])),
        branches: statics,
        branchesById,
        statuses: share(prev.statuses, statuses),
        tasks: share(prev.tasks, tasks),
        catalog: prev.catalog && same(prev.catalog, snapshot.catalog) ? prev.catalog : snapshot.catalog,
        filters: same(prev.filters, snapshot.filters) ? prev.filters : snapshot.filters,
        untracked: same(prev.untracked, snapshot.untracked) ? prev.untracked : snapshot.untracked,
        // A restarted server counts from 0 again: take the snapshot's.
        version: snapshot.version,
        updatedAt: snapshot.age === null ? prev.updatedAt : now - snapshot.age * 1000,
      });
    },

    setSnapshotError(message) {
      set({ error: message });
    },

    statusChanged(branchId, status, v) {
      const state = get();
      // Already reflected in the snapshot this page was built from.
      if (state.version !== null && v <= state.version) return;
      const before = state.statuses[branchId];
      set({
        version: v,
        updatedAt: clock(),
        statuses:
          before && same(before, status) ? state.statuses : { ...state.statuses, [branchId]: status },
      });
    },

    taskChanged(branchId, task, v) {
      const state = get();
      if (state.version !== null && v <= state.version) return;
      const before = state.tasks[branchId];
      set({
        version: v,
        tasks: before && same(before, task) ? state.tasks : { ...state.tasks, [branchId]: task },
      });
    },

    refreshed(v) {
      const state = get();
      if (state.version !== null && v <= state.version) return;
      set({ version: v, updatedAt: clock() });
    },

    untrackedChanged(untracked, v) {
      const state = get();
      if (state.version !== null && v <= state.version) return;
      set({ version: v, untracked });
    },

    hello(v, ageSeconds) {
      set({
        connection: "live",
        version: v,
        updatedAt: ageSeconds === null ? null : clock() - ageSeconds * 1000,
      });
    },

    setConnection(connection) {
      set({ connection });
    },

    setView(view) {
      if (view.compact !== get().view.compact) preferences.saveCompact(view.compact);
      set({ view });
    },

    focusRoot(root, view) {
      const current = get().view;
      get().setView({ ...current, root, ...(view ? { view } : {}) });
    },

    patchCardUi(id, patch) {
      set((state) => ({ cardUi: { ...state.cardUi, [id]: { ...state.cardUi[id], ...patch } } }));
    },

    toggleLog(id) {
      const open = get().logOpen;
      const next = open.includes(id) ? open.filter((k) => k !== id) : [...open, id];
      preferences.saveLogOpen(next);
      set({ logOpen: next });
    },
    toggleFold(id) {
      const folded = get().folded;
      const next = folded.includes(id) ? folded.filter((key) => key !== id) : [...folded, id];
      preferences.saveFolded(next);
      set({ folded: next });
    },

    toggleSection(key) {
      const collapsed = get().collapsed;
      const next = collapsed.includes(key)
        ? collapsed.filter((k) => k !== key)
        : [...collapsed, key];
      preferences.saveCollapsed(next);
      set({ collapsed: next });
    },

    expandSection(key) {
      const collapsed = get().collapsed;
      if (!collapsed.includes(key)) return;
      const next = collapsed.filter((k) => k !== key);
      preferences.saveCollapsed(next);
      set({ collapsed: next });
    },
  }));
}
