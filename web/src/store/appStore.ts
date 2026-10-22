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
  AgentConversation,
  Agents,
  BranchExtras,
  CardExtras,
  FetchedBranch,
  RepoWebhook,
  MergeQueueInfo,
  SlackInfo,
  Spend,
  Branch,
  BranchClaim,
  Catalog,
  ClaimPatch,
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
export type StaticBranch = Omit<Branch, "status" | "task_live" | "claim" | "slack" | "merge_queue">;

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
  /** Who holds a claim on each branch (live holders only). */
  claims: Record<string, BranchClaim>;
  /** Where each branch's PR stands in Slack (absent: Slack is off, or no PR yet). */
  slacks: Record<string, SlackInfo>;
  /** Where each branch's PR stands in the merge queue (absent: nothing to say). */
  mergeQueues: Record<string, MergeQueueInfo>;
  /** Only for branches whose task the configured tracker owns. */
  tasks: Record<string, TaskLive>;
  /** When (ms) each branch's PR status was last looked up on GitHub. */
  fetchedAt: Record<string, number>;
  /** The repos (by id) the user administers whose GitHub webhook is not working. */
  webhooks: Record<number, RepoWebhook>;
  /** Why a branch's latest lookup failed (what its card shows is then the last good one). */
  fetchError: Record<string, string>;
  /** What the cards show beyond the PR status (the agents' view of each
   * branch and what each task has cost), pushed by the server. */
  extras: Record<string, BranchExtras>;
  /** When (ms) the agents last decided every task and spend was last read:
   * how old the Agent and Spend boxes' data is. */
  decidedAt: number | null;
  spendReadAt: number | null;
  /** The Agents view's data, pushed (``null`` until the server has worked it out). */
  agentsView: Agents | null;
  /** The Spend view's data by window (days), pushed. */
  spendViews: Record<number, Spend>;
  /** The conversation of each run that was opened or is working, by run id. */
  conversations: Record<string, AgentConversation>;
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
  statusChanged(
    branchId: string,
    status: LiveStatus,
    v: number,
    slack?: SlackInfo | null,
    mergeQueue?: MergeQueueInfo | null,
  ): void;
  taskChanged(branchId: string, task: TaskLive, v: number): void;
  /** Claims changed on some branches: their claim and re-derived status. */
  claimsChanged(patches: ClaimPatch[], v: number): void;
  /** A polling pass finished; ``fetched`` are the branches it looked up. */
  refreshed(v: number, fetched: FetchedBranch[]): void;
  untrackedChanged(untracked: UntrackedPrs, v: number): void;
  extrasChanged(extras: CardExtras, v?: number): void;
  agentsChanged(agents: Agents, v?: number): void;
  spendChanged(spend: Spend[], v?: number): void;
  webhooksChanged(webhooks: RepoWebhook[], v?: number): void;
  conversationChanged(conversation: AgentConversation, v?: number): void;
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

/** The store's parts of the snapshot's lookup times and errors. */
function fetchedState(fetched: FetchedBranch[]): Pick<AppState, "fetchedAt" | "fetchError"> {
  const fetchedAt: Record<string, number> = {};
  const fetchError: Record<string, string> = {};
  for (const f of fetched) {
    if (f.fetched_at !== null) fetchedAt[f.branch_id] = Date.parse(f.fetched_at);
    if (f.error !== null) fetchError[f.branch_id] = f.error;
  }
  return { fetchedAt, fetchError };
}

/** The store's parts of a ``CardExtras`` payload. */
function extrasState(extras: CardExtras): Pick<AppState, "extras" | "decidedAt" | "spendReadAt"> {
  return {
    extras: Object.fromEntries(extras.branches.map((branch) => [branch.id, branch])),
    decidedAt: extras.agents_decided_at ? Date.parse(extras.agents_decided_at) : null,
    spendReadAt: extras.spend_read_at ? Date.parse(extras.spend_read_at) : null,
  };
}

const webhooksByRepo = (webhooks: RepoWebhook[]): Record<number, RepoWebhook> =>
  Object.fromEntries(webhooks.map((webhook) => [webhook.repo_id, webhook]));

const spendByWindow = (spend: Spend[]): Record<number, Spend> =>
  Object.fromEntries(spend.map((s) => [s.breakdown_days, s]));

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
    claims: {},
    slacks: {},
    mergeQueues: {},
    tasks: {},
    fetchedAt: {},
    fetchError: {},
    webhooks: {},
    extras: {},
    decidedAt: null,
    spendReadAt: null,
    agentsView: null,
    spendViews: {},
    conversations: {},
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
      const claims: Record<string, BranchClaim> = {};
      const slacks: Record<string, SlackInfo> = {};
      const mergeQueues: Record<string, MergeQueueInfo> = {};
      const statics: StaticBranch[] = [];
      for (const { status, task_live, claim, slack, merge_queue, ...rest } of snapshot.branches) {
        statuses[rest.id] = status;
        claims[rest.id] = claim;
        if (slack) slacks[rest.id] = slack;
        if (merge_queue) mergeQueues[rest.id] = merge_queue;
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
        claims: share(prev.claims, claims),
        slacks: share(prev.slacks, slacks),
        mergeQueues: share(prev.mergeQueues, mergeQueues),
        ...fetchedState(snapshot.fetched),
        webhooks: webhooksByRepo(snapshot.webhooks),
        ...(snapshot.extras ? extrasState(snapshot.extras) : {}),
        agentsView: snapshot.agents ?? prev.agentsView,
        // A conversation that was still working may have moved on (or ended)
        // while events were missed: it is asked for again when it is shown.
        conversations: Object.fromEntries(Object.entries(prev.conversations).filter(([, c]) => !c.running)),
        spendViews: snapshot.spend.length > 0 ? spendByWindow(snapshot.spend) : prev.spendViews,
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

    statusChanged(branchId, status, v, slack, mergeQueue) {
      const state = get();
      // Already reflected in the snapshot this page was built from.
      if (state.version !== null && v <= state.version) return;
      const before = state.statuses[branchId];
      let slacks = state.slacks;
      if (slack !== undefined && !same(slacks[branchId] ?? null, slack)) {
        slacks = { ...slacks };
        if (slack === null) delete slacks[branchId];
        else slacks[branchId] = slack;
      }
      let mergeQueues = state.mergeQueues;
      if (mergeQueue !== undefined && !same(mergeQueues[branchId] ?? null, mergeQueue)) {
        mergeQueues = { ...mergeQueues };
        if (mergeQueue === null) delete mergeQueues[branchId];
        else mergeQueues[branchId] = mergeQueue;
      }
      set({
        version: v,
        updatedAt: clock(),
        statuses:
          before && same(before, status) ? state.statuses : { ...state.statuses, [branchId]: status },
        slacks,
        mergeQueues,
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

    claimsChanged(patches, v) {
      const state = get();
      if (state.version !== null && v <= state.version) return;
      const statuses = { ...state.statuses };
      const claims = { ...state.claims };
      for (const patch of patches) {
        if (!(patch.branch_id in state.branchesById)) continue;
        if (!same(statuses[patch.branch_id], patch.status)) statuses[patch.branch_id] = patch.status;
        if (!same(claims[patch.branch_id], patch.claim)) claims[patch.branch_id] = patch.claim;
      }
      set({ version: v, statuses, claims });
    },

    refreshed(v, fetched) {
      const state = get();
      if (state.version !== null && v <= state.version) return;
      const fetchedAt = { ...state.fetchedAt };
      const fetchError = { ...state.fetchError };
      for (const f of fetched) {
        if (f.fetched_at !== null) fetchedAt[f.branch_id] = Date.parse(f.fetched_at);
        if (f.error === null) delete fetchError[f.branch_id];
        else fetchError[f.branch_id] = f.error;
      }
      set({ version: v, updatedAt: clock(), fetchedAt, fetchError });
    },

    extrasChanged(extras, v) {
      const state = get();
      if (v !== undefined && state.version !== null && v <= state.version) return;
      set({ ...(v === undefined ? {} : { version: v }), ...extrasState(extras) });
    },

    agentsChanged(agents, v) {
      const state = get();
      if (v !== undefined && state.version !== null && v <= state.version) return;
      set({ ...(v === undefined ? {} : { version: v }), agentsView: agents });
    },

    webhooksChanged(webhooks, v) {
      const state = get();
      if (v !== undefined && state.version !== null && v <= state.version) return;
      set({ ...(v === undefined ? {} : { version: v }), webhooks: webhooksByRepo(webhooks) });
    },

    spendChanged(spend, v) {
      const state = get();
      if (v !== undefined && state.version !== null && v <= state.version) return;
      set({
        ...(v === undefined ? {} : { version: v }),
        spendViews: { ...state.spendViews, ...spendByWindow(spend) },
      });
    },

    conversationChanged(conversation, v) {
      const state = get();
      if (v !== undefined && state.version !== null && v <= state.version) return;
      set({
        ...(v === undefined ? {} : { version: v }),
        conversations: { ...state.conversations, [conversation.run_id]: conversation },
      });
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
