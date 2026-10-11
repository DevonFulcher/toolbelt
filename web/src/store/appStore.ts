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
} from "../api/generated";
import type { ConnectionState } from "../live/connection";
import { DEFAULT_STATE, type ViewState } from "../logic/viewState";

/** A branch without its live parts (those are patched separately). */
export type StaticBranch = Omit<Branch, "status" | "task_live">;

/** Per-card timeline UI state, kept across live updates. */
export interface CardUi {
  expanded?: boolean;
  hideCommits?: boolean;
  legend?: boolean;
}

/** Remembered between visits (a convenience only: the URL is the state). */
export interface Preferences {
  loadCompact(): string | null;
  saveCompact(value: "" | "1"): void;
  loadCollapsed(): string[];
  saveCollapsed(keys: string[]): void;
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
  /** The latest event version seen (the snapshot's, then each event's). */
  version: number | null;
  /** `Date.now()` of the last refresh we know about. */
  updatedAt: number | null;
  connection: ConnectionState;

  view: ViewState;
  cardUi: Record<string, CardUi>;
  /** Keys of the collapsed card sections. */
  collapsed: string[];

  // -- actions --
  applySnapshot(snapshot: Snapshot, now?: number): void;
  setSnapshotError(message: string): void;
  statusChanged(branchId: string, status: LiveStatus, v: number): void;
  taskChanged(branchId: string, task: TaskLive, v: number): void;
  refreshed(v: number): void;
  hello(v: number, ageSeconds: number | null): void;
  setConnection(state: ConnectionState): void;
  /** Replaces the view state (the URL follows). */
  setView(view: ViewState): void;
  patchCardUi(id: string, patch: CardUi): void;
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
    version: null,
    updatedAt: null,
    connection: "connecting",
    view: init.view ?? DEFAULT_STATE,
    cardUi: {},
    collapsed: preferences.loadCollapsed(),

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
      set({
        ready: true,
        error: null,
        repos: same(prev.repos, snapshot.repos) ? prev.repos : snapshot.repos,
        reposById: Object.fromEntries(snapshot.repos.map((repo) => [repo.id, repo])),
        branches: statics,
        branchesById: Object.fromEntries(statics.map((b) => [b.id, b])),
        statuses: share(prev.statuses, statuses),
        tasks: share(prev.tasks, tasks),
        catalog: prev.catalog && same(prev.catalog, snapshot.catalog) ? prev.catalog : snapshot.catalog,
        filters: same(prev.filters, snapshot.filters) ? prev.filters : snapshot.filters,
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

    patchCardUi(id, patch) {
      set((state) => ({ cardUi: { ...state.cardUi, [id]: { ...state.cardUi[id], ...patch } } }));
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
