import { agents } from "../testing/agentsFixtures";
import { spend } from "../testing/spendFixtures";
import { describe, expect, test } from "vitest";
import type { LiveStatus, ServerEvent, Snapshot } from "../api/generated";
import { createAppStore } from "../store/appStore";
import { branch, noPr, pr, snapshot } from "../testing/fixtures";
import { GRACE_MS, RECONNECT_DELAY_MS } from "./connection";
import { startLiveUpdates, type EventSourceLike } from "./stream";

class FakeEventSource implements EventSourceLike {
  readyState = 1;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;
  private listeners = new Map<string, ((event: { data: string }) => void)[]>();

  addEventListener(type: string, listener: (event: { data: string }) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  close(): void {
    this.closed = true;
    this.readyState = 2;
  }
  emit(event: ServerEvent): void {
    for (const listener of this.listeners.get(event.type) ?? []) {
      listener({ data: JSON.stringify(event) });
    }
  }
}

/** Lets queued promise callbacks run. */
const settle = async (): Promise<void> => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

function setup(options: { visible?: boolean; snapshots?: Snapshot[] } = {}) {
  const env = {
    visible: options.visible ?? true,
    time: 0,
    fetches: 0,
    sources: [] as FakeEventSource[],
    snapshots: options.snapshots ?? [snapshot([branch(1, "a")])],
    timers: [] as { fn: () => void; at: number; live: boolean }[],
  };
  const store = createAppStore({ clock: () => env.time });
  const live = startLiveUpdates({
    store,
    fetchSnapshot: async () => {
      const index = Math.min(env.fetches, env.snapshots.length - 1);
      env.fetches += 1;
      return env.snapshots[index];
    },
    openStream: () => {
      const source = new FakeEventSource();
      env.sources.push(source);
      return source;
    },
    isVisible: () => env.visible,
    now: () => env.time,
    setTimer: (fn, ms) => {
      const timer = { fn, at: env.time + ms, live: true };
      env.timers.push(timer);
      return timer;
    },
    clearTimer: (handle) => {
      (handle as { live: boolean }).live = false;
    },
  });
  const advance = (ms: number): void => {
    env.time += ms;
    for (const timer of env.timers) {
      if (timer.live && timer.at <= env.time) {
        timer.live = false;
        timer.fn();
      }
    }
  };
  const current = (): FakeEventSource => env.sources[env.sources.length - 1];
  return { env, store, live, advance, current };
}

const loaded: LiveStatus = pr({ pr_number: 7 });

describe("live updates", () => {
  test("loads the snapshot, then applies the stream's events", async () => {
    const { env, store, current } = setup();
    await settle();

    expect(store.getState().ready).toBe(true);
    expect(store.getState().branches.map((b) => b.name)).toEqual(["a"]);
    expect(env.fetches).toBe(1);

    current().emit({ type: "hello", v: 0, age: null, build: null });
    await settle();
    expect(store.getState().connection).toBe("live");
    // The version matched the snapshot's: nothing was missed.
    expect(env.fetches).toBe(1);

    env.time = 5000;
    current().emit({ type: "status", v: 1, branch_id: "1:a", status: loaded, slack: null, merge_queue: null, sibling_conflicts: null });
    current().emit({ type: "refreshed", v: 2, fetched: [{ branch_id: "1:a", fetched_at: "2026-10-17T12:00:00Z", error: null }] });
    await settle();
    expect(store.getState().statuses["1:a"]).toEqual(loaded);
    expect(store.getState().updatedAt).toBe(5000);
    expect(store.getState().fetchedAt).toEqual({ "1:a": Date.parse("2026-10-17T12:00:00Z") });
    expect(store.getState().fetchedAt).toEqual({ "1:a": Date.parse("2026-10-17T12:00:00Z") });
    expect(store.getState().version).toBe(2);
  });

  test("a hello whose version moved on re-fetches the snapshot", async () => {
    const { env, store, current } = setup({
      snapshots: [snapshot([branch(1, "a")]), snapshot([branch(1, "a"), branch(1, "b")], undefined, { version: 9 })],
    });
    await settle();

    current().emit({ type: "hello", v: 9, age: 3, build: null });
    await settle();

    expect(env.fetches).toBe(2);
    expect(store.getState().branches.map((b) => b.name)).toEqual(["a", "b"]);
    expect(store.getState().version).toBe(9);
  });

  test("a structure event re-fetches the snapshot", async () => {
    const { env, store, current } = setup({
      snapshots: [snapshot([branch(1, "a")]), snapshot([branch(1, "a"), branch(1, "b")], undefined, { version: 1 })],
    });
    await settle();
    current().emit({ type: "hello", v: 0, age: null, build: null });

    current().emit({ type: "structure", v: 1 });
    await settle();

    expect(env.fetches).toBe(2);
    expect(store.getState().branches).toHaveLength(2);
  });

  test("a status that arrives while the snapshot is re-fetched is applied after it, not lost", async () => {
    const { store, current } = setup({
      snapshots: [snapshot([branch(1, "a")]), snapshot([branch(1, "a")], undefined, { version: 1 })],
    });
    await settle();
    current().emit({ type: "hello", v: 0, age: null, build: null });

    current().emit({ type: "structure", v: 1 });
    current().emit({ type: "status", v: 2, branch_id: "1:a", status: loaded, slack: null, merge_queue: null, sibling_conflicts: null });
    await settle();

    expect(store.getState().statuses["1:a"]).toEqual(loaded);
  });

  test("events already reflected in the snapshot are dropped", async () => {
    const stale: LiveStatus = noPr();
    const { store, current } = setup({
      snapshots: [snapshot([branch(1, "a", { status: loaded })], undefined, { version: 5 })],
    });
    await settle();

    current().emit({ type: "hello", v: 5, age: null, build: null });
    current().emit({ type: "status", v: 4, branch_id: "1:a", status: stale, slack: null, merge_queue: null, sibling_conflicts: null });
    await settle();

    expect(store.getState().statuses["1:a"]).toEqual(loaded);
  });

  test("task events patch the branch's task", async () => {
    const { store, current } = setup();
    await settle();
    current().emit({ type: "hello", v: 0, age: null, build: null });

    current().emit({
      type: "task",
      v: 1,
      branch_id: "1:a",
      task: { state: "info", title: "Fix it", status: "Open" },
    });
    await settle();

    expect(store.getState().tasks["1:a"]).toEqual({ state: "info", title: "Fix it", status: "Open" });
  });

  test("an untracked event replaces the list of untracked PRs", async () => {
    const { store, current } = setup();
    await settle();
    current().emit({ type: "hello", v: 0, age: null, build: null });

    current().emit({ type: "untracked", v: 1, untracked: { state: "loaded", prs: [], truncated: false, hidden: 0 } });
    await settle();

    expect(store.getState().untracked).toEqual({ state: "loaded", prs: [], truncated: false, hidden: 0 });
  });

  test("a stream that died is reopened after a delay while the page is visible", async () => {
    const { env, store, advance, current } = setup();
    await settle();
    const first = current();
    first.emit({ type: "hello", v: 0, age: null, build: null });

    first.readyState = 2;
    first.onerror?.({});
    expect(store.getState().connection).toBe("disconnected");
    expect(env.sources).toHaveLength(1);

    advance(RECONNECT_DELAY_MS);
    expect(env.sources).toHaveLength(2);
  });

  test("a transient error shows reconnecting (the browser retries by itself)", async () => {
    const { store, current } = setup();
    await settle();

    current().onerror?.({});

    expect(store.getState().connection).toBe("reconnecting");
  });

  test("a page hidden past the grace period closes the stream, and reopens on return", async () => {
    const { env, store, live, advance, current } = setup();
    await settle();
    const first = current();

    env.visible = false;
    live.visibilityChanged();
    advance(GRACE_MS);
    expect(first.closed).toBe(true);
    expect(store.getState().connection).toBe("paused");

    env.visible = true;
    live.visibilityChanged();
    expect(env.sources).toHaveLength(2);
    expect(store.getState().connection).toBe("resuming");
    current().emit({ type: "hello", v: 0, age: null, build: null });
    await settle();
    expect(store.getState().connection).toBe("live");
  });

  test("a page loaded in a background tab fetches the snapshot but opens no stream", async () => {
    const { env, store, live } = setup({ visible: false });
    await settle();

    expect(store.getState().ready).toBe(true);
    expect(env.sources).toHaveLength(0);
    expect(store.getState().connection).toBe("paused");

    env.visible = true;
    live.visibilityChanged();
    expect(env.sources).toHaveLength(1);
  });

  test("a failed snapshot is reported", async () => {
    const store = createAppStore();
    startLiveUpdates({
      store,
      fetchSnapshot: async () => {
        throw new Error("HTTP 500");
      },
      openStream: () => new FakeEventSource(),
      isVisible: () => true,
      now: () => 0,
      setTimer: () => 0,
      clearTimer: () => {},
    });
    await settle();

    expect(store.getState().ready).toBe(false);
    expect(store.getState().error).toContain("HTTP 500");
  });
});

test("a claims event applies each listed branch's claim and status", async () => {
  const { store, current } = setup();
  await settle();
  current().emit({ type: "hello", v: 0, age: null, build: null });
  const claimed = pr({
    next: { label: "With an agent", who: "agent", kind: "claimed", would_be: null },
  });

  current().emit({
    type: "claims",
    v: 1,
    branches: [
      {
        branch_id: "1:a",
        claim: {
          holders: [
            {
              holder: "s1",
              kind: "agent",
              state: "idle",
              link: { label: "Open session", href: "claude://x" },
              age: { text: "<1m", since: null },
              stale: false,
            },
          ],
        },
        status: claimed,
      },
    ],
  });
  await settle();

  expect(store.getState().statuses["1:a"]).toEqual(claimed);
  expect(store.getState().claims["1:a"].holders[0].state).toBe("idle");
});

test("the pushed views reach the store: extras, agents, spend, webhooks and a conversation", async () => {
  const { store, current } = setup();
  await settle();
  current().emit({ type: "hello", v: 0, age: null, build: null });

  current().emit({
    type: "extras",
    v: 1,
    extras: { branches: [], agents_decided_at: "2026-10-17T12:00:00Z", spend_read_at: null },
  });
  current().emit({ type: "agents", v: 2, agents: agents({ enabled: false }) });
  current().emit({ type: "spend", v: 3, spend: [spend()] });
  current().emit({
    type: "webhooks",
    v: 4,
    webhooks: [{ repo_id: 1, state: "blocked", problem: "The webhook is taken." }],
  });
  current().emit({
    type: "conversation",
    v: 5,
    conversation: { run_id: "r1", running: true, messages: [] },
  });
  await settle();

  const state = store.getState();
  expect(state.decidedAt).toBe(Date.parse("2026-10-17T12:00:00Z"));
  expect(state.agentsView?.enabled).toBe(false);
  expect(Object.keys(state.spendViews)).toEqual(["7"]);
  expect(state.webhooks[1]?.state).toBe("blocked");
  expect(state.conversations["r1"].running).toBe(true);
  expect(state.version).toBe(5);
});
