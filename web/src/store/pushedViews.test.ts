import { describe, expect, test } from "vitest";
import { agents } from "../testing/agentsFixtures";
import { branch, pr, snapshot } from "../testing/fixtures";
import { spend } from "../testing/spendFixtures";
import type { AgentConversation, BranchExtras, CardExtras } from "../api/generated";
import { createAppStore } from "./appStore";

const extrasOf = (id: string, decided: string | null = null): CardExtras => ({
  branches: [
    {
      id,
      agent: { has_action: true, trigger: "fix_ci", reason: null, can_override: false, starting: false, start_error: null, latest_run: null, runs: 0, recent_runs: [] },
      spend: { interactive_usd: 1, interactive_unpriced_calls: 0, autonomous_usd: 0, autonomous_runs: 0 },
    } satisfies BranchExtras,
  ],
  agents_decided_at: decided,
  spend_read_at: null,
});

const conversation = (running: boolean, texts: string[], runId = "r1"): AgentConversation => ({
  run_id: runId,
  running,
  messages: texts.map((text) => ({ kind: "text", at: "t", text, tool: null, is_error: false })),
});

describe("the views the server pushes", () => {
  test("a snapshot carries what the server had worked out when the page opened", () => {
    const store = createAppStore();

    store.getState().applySnapshot(
      snapshot([branch(1, "a", { status: pr() })], undefined, {
        extras: extrasOf("1:a", "2026-10-17T12:00:00Z"),
        agents: agents(),
        spend: [spend()],
      }),
    );

    const state = store.getState();
    expect(Object.keys(state.extras)).toEqual(["1:a"]);
    expect(state.decidedAt).toBe(Date.parse("2026-10-17T12:00:00Z"));
    expect(state.agentsView?.enabled).toBe(true);
    expect(Object.keys(state.spendViews)).toEqual(["7"]);
  });

  test("a snapshot from before the server had worked them out keeps what the page has", () => {
    const store = createAppStore();
    store.getState().applySnapshot(snapshot([branch(1, "a")], undefined, { agents: agents(), spend: [spend()] }));

    store.getState().applySnapshot(snapshot([branch(1, "a")]));

    expect(store.getState().agentsView).not.toBeNull();
    expect(Object.keys(store.getState().spendViews)).toEqual(["7"]);
  });

  test("an event replaces the card extras, the agents view and one window of spend", () => {
    const store = createAppStore();
    store.getState().applySnapshot(snapshot([branch(1, "a")], undefined, { version: 1 }));

    store.getState().extrasChanged(extrasOf("1:a"), 2);
    store.getState().agentsChanged(agents({ enabled: false }), 3);
    store.getState().spendChanged([spend({ breakdown_days: 1 }), spend({ breakdown_days: 30 })], 4);

    const state = store.getState();
    expect(state.extras["1:a"].spend.interactive_usd).toBe(1);
    expect(state.agentsView?.enabled).toBe(false);
    expect(Object.keys(state.spendViews).sort()).toEqual(["1", "30"]);
    expect(state.version).toBe(4);
  });

  test("an event the snapshot already reflects is ignored", () => {
    const store = createAppStore();
    store.getState().applySnapshot(snapshot([branch(1, "a")], undefined, { version: 5, agents: agents() }));

    store.getState().agentsChanged(agents({ enabled: false }), 5);

    expect(store.getState().agentsView?.enabled).toBe(true);
  });

  test("a working run's conversation grows with each event, and a reload drops it to be asked for again", () => {
    const store = createAppStore();
    store.getState().applySnapshot(snapshot([branch(1, "a")], undefined, { version: 1 }));
    store.getState().conversationChanged(conversation(true, ["one"]), 2);
    store.getState().conversationChanged(conversation(true, ["one", "two"]), 3);
    store.getState().conversationChanged(conversation(false, ["done"], "finished"), 4);

    expect(store.getState().conversations["r1"].messages).toHaveLength(2);

    store.getState().applySnapshot(snapshot([branch(1, "a")], undefined, { version: 9 }));

    expect(Object.keys(store.getState().conversations)).toEqual(["finished"]);
  });
});

describe("the repos whose webhook is not working", () => {
  const blocked = { repo_id: 1, state: "blocked", problem: "The webhook is taken." } as const;

  test("come with the snapshot and are replaced by each event", () => {
    const store = createAppStore();
    store.getState().applySnapshot(snapshot([branch(1, "devon/a")], undefined, { webhooks: [blocked] }));
    expect(store.getState().webhooks).toEqual({ 1: blocked });

    store.getState().webhooksChanged([], 5);

    expect(store.getState().webhooks).toEqual({});
    expect(store.getState().version).toBe(5);
  });

  test("an event older than the snapshot is ignored", () => {
    const store = createAppStore();
    store.getState().applySnapshot(snapshot([branch(1, "devon/a")], undefined, { version: 9 }));

    store.getState().webhooksChanged([blocked], 4);

    expect(store.getState().webhooks).toEqual({});
  });
});
