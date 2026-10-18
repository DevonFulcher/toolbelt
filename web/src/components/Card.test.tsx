import { act, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { Branch, BranchClaim, BranchExtras, LoadedStatus } from "../api/generated";
import { branch, catalog, detail, event, noPr, pr, repo, snapshot, DAY, NOW } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

// A spy on the pills each card renders: a card that re-renders re-renders
// its pills, so counting by their text shows which cards re-rendered.
const pillRenders = vi.hoisted(() => new Map<string, number>());
vi.mock("./presentation", async (original) => {
  const actual = await original<typeof import("./presentation")>();
  return {
    ...actual,
    Pill: (props: Parameters<typeof actual.Pill>[0]) => {
      pillRenders.set(props.children, (pillRenders.get(props.children) ?? 0) + 1);
      return actual.Pill(props);
    },
  };
});

const SLOT_LABELS: Record<string, string> = {
  repo: "Repo",
  branch: "Branch",
  next: "Next",
  pr: "PR",
  ci: "CI",
  review: "Review",
  comments: "Human comments",
  bot_comments: "Bot comments",
  merge: "Merge",
  conflict_commit: "Conflict commit",
  lines: "Lines",
  task: "Task",
  session: "Interactive session",
  agent_status: "Autonomous agent",
  agent_run: "Latest agent run",
  agent_run_summary: "Latest run summary",
  cost: "Cost",
  agent_cost: "Autonomous agent cost",
  ancestors: "Ancestors",
  descendants: "Descendants",
  task_age: "Task age",
  pr_age: "PR age",
  in_draft: "In draft",
  review_requested: "Review requested",
  ci_failures: "CI failures",
  reviewers: "Reviewers",
  title: "PR title",
  timeline: "Timeline",
};

function showCards(branches: Branch[], options: Parameters<typeof renderWith>[2] = {}, repos = [repo(1, "api")]) {
  return renderWith(<App clock={() => NOW} />, snapshot(branches, repos), options);
}

/** `{slot: [label, state, text]}` for one card. */
function slotsOf(name: string): Record<string, [string, string, string]> {
  const card = [...document.querySelectorAll<HTMLElement>("article.card")].find((el) =>
    within(el).queryByText(name, { selector: ".branch" }),
  );
  expect(card, `card of ${name}`).toBeTruthy();
  const out: Record<string, [string, string, string]> = {};
  for (const slot of (card as HTMLElement).querySelectorAll<HTMLElement>(".slot")) {
    const state = [...slot.classList].find((c) => c.startsWith("v-"))?.slice(2) ?? "";
    const label = slot.querySelector(".slot-label")?.textContent ?? "";
    const value = (slot.querySelector(".slot-value")?.textContent ?? "").replace(/\s+/g, " ").trim();
    out[slot.dataset.slot as string] = [label, state, value];
  }
  return out;
}

const labels = (slots: Record<string, [string, string, string]>) =>
  Object.fromEntries(Object.entries(slots).map(([key, [label]]) => [key, label]));

describe("a card's labelled slots", () => {
  test("a populated card labels every value", () => {
    const full: LoadedStatus = pr({
      pr_number: 8,
      pr_url: "https://github.com/o/r/pull/8",
      review: "requested",
      open_comments: 3,
      open_comments_bot: 1,
      merge: "conflicts",
      conflict: {
        sha: "abcdef1234567",
        subject: "Rewrite the loader",
        url: "https://github.com/o/r/commit/abcdef1234567",
        files: ["src/a.py", "src/b.py", "src/c.py", "src/d.py"],
        source: "pr",
        step: null,
        steps: null,
      },
      next: { label: "Resolve conflicts", who: "you", kind: "resolve_conflicts" },
      detail: detail({
        title: "Add <b>the</b> thing",
        pr_age: { text: "5d", since: new Date(NOW - 5 * DAY).toISOString() },
        review_request_age: { text: "1d", since: new Date(NOW - DAY).toISOString() },
        ci_failures: 2,
        reviewers: ["sam", "lee"],
        additions: 120,
        deletions: 4,
      }),
    });
    const draft = pr({
      pr: "draft",
      pr_number: 7,
      ci: "failed",
      next: { label: "Fix / re-run CI", who: "you", kind: "fix_ci" },
      detail: detail({
        is_draft: true,
        draft_age: { text: "5d", since: new Date(NOW - 5 * DAY).toISOString() },
      }),
    });
    showCards([
      branch(1, "devon/a", {
        status: draft,
        children: [{ id: "1:devon/b", name: "devon/b" }],
        stack_pos: 1,
        stack_height: 2,
        task: { label: "ABC-1", href: "https://tasks.example/abc-1" },
        agent: { label: "Open session", href: "claude://x" },
      }),
      branch(1, "devon/b", { parent: "devon/a", status: full }),
    ]);

    const b = slotsOf("devon/b");
    const a = slotsOf("devon/a");

    expect(labels(b)).toEqual(SLOT_LABELS);
    expect(b.next[2]).toBe("Resolve conflicts needs you");
    expect(b.pr).toEqual(["PR", "ok", "#8 open"]);
    expect(b.ci[2]).toBe("success");
    expect(b.review[2]).toBe("requested");
    expect(b.comments).toEqual(["Human comments", "ok", "2 comments"]);
    expect(b.bot_comments).toEqual(["Bot comments", "ok", "1 comment"]);
    expect(b.merge[2]).toBe("conflicts");
    expect(b.conflict_commit[1]).toBe("ok");
    expect(b.conflict_commit[2]).toContain("abcdef1 Rewrite the loader");
    expect(b.conflict_commit[2]).toContain("first commit that conflicts with the base: src/a.py, src/b.py, src/c.py and 1 more");
    expect(document.querySelector('article[data-id="1:devon/b"] [data-slot="conflict_commit"] a')).toHaveAttribute(
      "href",
      "https://github.com/o/r/commit/abcdef1234567",
    );
    expect(a.conflict_commit).toEqual(["Conflict commit", "na", "no conflict"]);
    expect(b.pr_age[2]).toBe("5d");
    expect(b.lines[2]).toBe("+120 -4");
    expect(document.querySelector('article[data-id="1:devon/b"] [data-slot="lines"] .lines-added')).toHaveTextContent("+120");
    expect(document.querySelector('article[data-id="1:devon/b"] [data-slot="lines"] .lines-deleted')).toHaveTextContent("-4");
    expect(b.in_draft).toEqual(["In draft", "na", "not a draft"]);
    expect(b.review_requested).toEqual(["Review requested", "ok", "1d"]);
    expect(b.ci_failures[2]).toBe("2 failures");
    expect(document.querySelector('article[data-id="1:devon/b"] [data-slot="ci_failures"] .pill')).toHaveClass("tone-red");
    expect(b.reviewers).toEqual(["Reviewers", "ok", "sam, lee"]);
    expect(b.ancestors[2]).toBe("1");
    expect(b.descendants[2]).toBe("0");
    expect(within(document.querySelector('article[data-id="1:devon/b"]') as HTMLElement).getByRole("link", { name: "open in stacked view →" })).toBeInTheDocument();
    // The title is text, never markup.
    expect(b.title[2]).toBe("Add <b>the</b> thing");
    expect(b.task[1]).toBe("gap");
    expect(b.task[2]).toBe("no task linked");

    expect(a.task).toEqual(["Task", "ok", "ABC-1"]);
    expect(a.session[1]).toBe("ok");
    expect(a.in_draft[2]).toBe("5d");
    expect(a.ancestors[2]).toBe("0");
    expect(a.descendants[2]).toBe("1");
  });

  test("the task age counts from when the task started, and says so when unknown", () => {
    showCards([
      branch(1, "devon/a", { started_at: new Date(NOW - 8 * DAY).toISOString(), status: noPr() }),
      branch(1, "devon/b", { status: noPr() }),
    ]);

    expect(slotsOf("devon/a").task_age).toEqual(["Task age", "ok", "8d"]);
    expect(slotsOf("devon/b").task_age).toEqual(["Task age", "gap", "no start recorded"]);
  });

  test("the task age is not shown once the PR is merged", () => {
    showCards(
      [
        branch(1, "devon/m", {
          started_at: new Date(NOW - 8 * DAY).toISOString(),
          status: pr({ pr: "merged", merge: "none", review: "approved", next: { label: "Done", who: "none", kind: "done" } }),
        }),
      ],
      { search: "?done=1" },
    );

    expect(slotsOf("devon/m").task_age).toEqual(["Task age", "na", "n/a (PR merged)"]);
  });

  test("a card without a PR still has every slot, each saying why it is empty", () => {
    showCards([branch(1, "devon/a", { status: noPr() })]);

    const found = slotsOf("devon/a");

    expect(labels(found)).toEqual(SLOT_LABELS);
    expect(found.next[2]).toBe("Open PR needs you");
    expect(found.pr).toEqual(["PR", "gap", "no PR"]);
    for (const key of ["ci", "review", "comments", "bot_comments", "merge", "conflict_commit", "lines", "pr_age", "in_draft", "review_requested", "ci_failures", "reviewers", "title"]) {
      expect(found[key].slice(1), key).toEqual(["na", "no PR"]);
    }
    // No start recorded and no PR: nothing to draw, and no chip row repeating
    // the slots above.
    expect(found.timeline[1]).toBe("ok");
    expect(found.timeline[2]).toContain("no PR yet: no history to draw");
    expect(found.task_age).toEqual(["Task age", "gap", "no start recorded"]);
    expect(found.timeline[2]).not.toContain("Open PR");
    // Missing but settable reads as a gap, not as "not applicable".
    expect(found.task).toEqual(["Task", "gap", "no task linked"]);
    expect(found.session).toEqual(["Interactive session", "gap", "no session linked"]);
    expect(found.ancestors[2]).toBe("0");
    expect(found.descendants[2]).toBe("0");
  });

  test("zero and empty values are explicit", () => {
    showCards(
      [
        branch(1, "devon/q", {
          status: pr({ ci: "none", pr_number: 9, detail: detail({ events: [event("opened", DAY)] }) }),
        }),
        branch(1, "devon/m", {
          status: pr({ pr: "merged", merge: "none", review: "approved", next: { label: "Done", who: "none", kind: "done" } }),
        }),
      ],
      { search: "?done=1" },
    );

    const found = slotsOf("devon/q");

    expect(labels(found)).toEqual(SLOT_LABELS);
    expect(found.ci).toEqual(["CI", "na", "no CI configured"]);
    expect(found.review).toEqual(["Review", "gap", "no review requested"]);
    expect(found.comments).toEqual(["Human comments", "ok", "0 comments"]);
    expect(found.bot_comments).toEqual(["Bot comments", "ok", "0 comments"]);
    expect(found.merge).toEqual(["Merge", "ok", "clean"]);
    expect(found.in_draft).toEqual(["In draft", "na", "not a draft"]);
    expect(found.review_requested).toEqual(["Review requested", "na", "not requested"]);
    expect(found.ci_failures).toEqual(["CI failures", "ok", "0 failures"]);
    const card = document.querySelector('article[data-id="1:devon/q"]') as HTMLElement;
    for (const key of ["comments", "bot_comments", "merge", "ci_failures"]) {
      expect(card.querySelector(`[data-slot="${key}"] .pill`)).toHaveClass("tone-green");
    }
    expect(found.reviewers).toEqual(["Reviewers", "na", "none requested"]);
    // A merged PR's merge slot says why it is empty.
    expect(slotsOf("devon/m").merge).toEqual(["Merge", "na", "n/a (PR merged)"]);
  });

  test("a PR without its history says the details are unavailable", () => {
    showCards([branch(1, "devon/a", { status: pr({ detail: null }) })]);

    const found = slotsOf("devon/a");

    expect(found.pr_age.slice(1)).toEqual(["na", "PR details unavailable"]);
    expect(found.title.slice(1)).toEqual(["na", "PR details unavailable"]);
    expect(found.timeline[2]).toContain("PR history unavailable");
  });

  test("slots are grouped into Git, PR, Task, Agent and Spend boxes, with the timeline below them", () => {
    showCards([
      branch(1, "devon/a", { status: pr({ detail: detail() }) }),
      branch(1, "devon/b"),
    ]);

    for (const card of document.querySelectorAll<HTMLElement>("article.card")) {
      const groups = [...card.querySelectorAll<HTMLElement>(":scope > .group")];
      expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual(["Git", "PR", "Task", "Agent", "Spend"]);
      expect(groups.map((g) => g.querySelector(".group-title > span")?.textContent)).toEqual(["Git", "PR", "Task", "Agent", "Spend"]);
      const keys = groups.map((g) => [...g.querySelectorAll<HTMLElement>(".slot")].map((s) => s.dataset.slot));
      expect(keys).toEqual([
        ["repo", "branch", "ancestors", "descendants"],
        ["title", "pr", "ci", "review", "comments", "bot_comments", "merge", "conflict_commit", "lines", "pr_age", "in_draft", "review_requested", "ci_failures", "reviewers"],
        ["next", "task_age", "task"],
        ["session", "agent_status", "agent_run", "agent_run_summary"],
        ["cost", "agent_cost"],
      ]);
      // The timeline is its own box, the last child, not one of the groups.
      const timeline = card.lastElementChild as HTMLElement;
      expect(timeline).toHaveClass("g-timeline");
      expect(timeline).not.toHaveClass("group");
      expect(timeline.querySelector('[data-slot="timeline"]')).not.toBeNull();
    }
  });

  test("an unloaded card has every slot saying loading", () => {
    showCards([branch(1, "devon/a")]);

    const found = slotsOf("devon/a");

    expect(labels(found)).toEqual(SLOT_LABELS);
    for (const key of Object.keys(SLOT_LABELS)) {
      if (["repo", "branch", "ancestors", "descendants", "task", "session", "task_age", "agent_status", "agent_run", "agent_run_summary", "cost", "agent_cost"].includes(key)) continue;
      expect(found[key].slice(1), key).toEqual(["na", "loading…"]);
    }
  });

  test("a failed lookup and a missing checkout each say so", () => {
    showCards(
      [
        branch(1, "devon/a", { status: { load: "failed" } }),
        branch(2, "devon/b", { status: { load: "loading" } }),
      ],
      {},
      [repo(1, "api"), repo(2, "gone", { path_missing: true })],
    );

    expect(slotsOf("devon/a").pr[2]).toBe("status unavailable");
    expect(screen.getAllByTitle("gh could not look up this branch's PR").length).toBeGreaterThan(0);
    expect(slotsOf("devon/b").pr[2]).toBe("repo path missing");
    expect(screen.getByText(/gone: checkout no longer exists at \/work\/gone/)).toBeInTheDocument();
  });

  test("every slot a card renders is one the catalog maps to a dimension", () => {
    showCards([
      branch(1, "devon/a", { status: pr({ detail: detail() }) }),
      branch(1, "devon/b", { status: noPr() }),
      branch(1, "devon/c"),
    ]);

    for (const name of ["devon/a", "devon/b", "devon/c"]) {
      for (const slot of Object.keys(slotsOf(name))) {
        expect(slot in catalog.slots, `slot ${slot} has no state mapping`).toBe(true);
      }
    }
  });

  test("the task slot shows the tracker's title and status, or why it is unavailable", () => {
    const withTask = (live: Branch["task_live"]) =>
      branch(1, "devon/a", {
        status: noPr(),
        task: { label: "ABC-1", href: "https://tasks.example/abc-1" },
        task_fetchable: true,
        task_live: live,
      });
    const { store } = showCards([withTask({ state: "loading" })]);
    expect(slotsOf("devon/a").task[2]).toBe("ABC-1");

    act(() => {
      store.getState().taskChanged("1:devon/a", { state: "info", title: "Fix it", status: "In Progress" }, 1);
    });
    expect(slotsOf("devon/a").task[2]).toBe("ABC-1 Fix it In Progress");

    act(() => {
      store.getState().taskChanged("1:devon/a", { state: "unavailable", reason: "JIRA_API_TOKEN not set" }, 2);
    });
    expect(screen.getByTitle("JIRA_API_TOKEN not set")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "ABC-1" })).toHaveAttribute("href", "https://tasks.example/abc-1");
  });
});

describe("live patches", () => {
  test("a status patch re-renders only that branch's card", () => {
    const a = branch(1, "devon/a", { status: pr({ ci: "success", pr_number: 1 }) });
    const b = branch(1, "devon/b", { status: pr({ ci: "running", pr_number: 2 }) });
    const { store } = showCards([a, b]);
    expect(slotsOf("devon/a").ci[2]).toBe("success");
    pillRenders.clear();

    act(() => {
      store.getState().statusChanged("1:devon/a", pr({ ci: "failed", pr_number: 1 }), 1);
    });

    expect(slotsOf("devon/a").ci[2]).toBe("failed");
    expect(pillRenders.get("failed")).toBeGreaterThan(0);
    // devon/b's card (its CI pill says "running") was not rendered again.
    expect(pillRenders.get("running")).toBeUndefined();
  });
});

describe("claims", () => {
  const holder = (over: Partial<BranchClaim["holders"][number]> = {}): BranchClaim["holders"][number] => ({
    holder: "s1",
    kind: "agent",
    state: "working",
    link: { label: "Open session", href: "claude://claude.ai/epitaxy/abc" },
    age: { text: "5m", since: null },
    stale: false,
    ...over,
  });
  const claimedStatus = pr({
    next: {
      label: "With an agent",
      who: "agent",
      kind: "claimed",
      would_be: { label: "Resolve comments", who: "you", kind: "resolve_comments" },
    },
  });

  test("a claimed branch says With an agent, what it would be, and links the session", () => {
    showCards([branch(1, "devon/a", { status: claimedStatus, claim: { holders: [holder()] } })]);

    const next = slotsOf("devon/a").next[2];

    expect(next).toContain("With an agent");
    expect(next).toContain("would be: Resolve comments");
    expect(next).toContain("agent working");
    expect(screen.getByRole("link", { name: "agent working" })).toHaveAttribute(
      "href",
      "claude://claude.ai/epitaxy/abc",
    );
  });

  test("an idle or blocked holder is a chip and leaves the next action alone", () => {
    showCards([
      branch(1, "devon/a", { status: pr(), claim: { holders: [holder({ state: "idle" })] } }),
      branch(1, "devon/b", { status: pr(), claim: { holders: [holder({ state: "blocked", holder: "s2" })] } }),
    ]);

    expect(slotsOf("devon/a").next[2]).toContain("Request review");
    expect(slotsOf("devon/a").next[2]).toContain("agent idle");
    expect(slotsOf("devon/b").next[2]).toContain("agent blocked on you");
  });

  test("a stale holder is flagged and a human claim says whose it is", () => {
    showCards([
      branch(1, "devon/a", { status: claimedStatus, claim: { holders: [holder({ stale: true })] } }),
      branch(1, "devon/b", {
        status: claimedStatus,
        claim: { holders: [holder({ kind: "human", holder: "human.devon", link: null })] },
      }),
    ]);

    expect(slotsOf("devon/a").next[2]).toContain("agent working (stale)");
    expect(slotsOf("devon/b").next[2]).toContain("claimed by you");
  });

  test("a claims patch moves the card to With an agent while it is shown", () => {
    const { store } = showCards([branch(1, "devon/a", { status: pr() })]);
    expect(slotsOf("devon/a").next[2]).toContain("Request review");

    act(() => {
      store.getState().claimsChanged(
        [{ branch_id: "1:devon/a", claim: { holders: [holder()] }, status: claimedStatus }],
        1,
      );
    });

    expect(slotsOf("devon/a").next[2]).toContain("With an agent");
    expect(screen.getByText("With an agent", { selector: ".cg-label" })).toBeInTheDocument();
  });
});

describe("a card's agent and spend slots", () => {
  const extras = (over: Partial<BranchExtras["agent"]> = {}, spend: Partial<BranchExtras["spend"]> = {}): BranchExtras => ({
    id: "1:devon/a",
    agent: {
      has_action: true,
      trigger: "fix_ci",
      reason: null,
      can_override: false,
      latest_run: null,
      runs: 0,
      recent_runs: over.latest_run ? [over.latest_run] : [],
      ...over,
    },
    spend: { interactive_usd: 1.5, interactive_unpriced_calls: 0, autonomous_usd: 0.05, autonomous_runs: 2, ...spend },
  });
  const show = (data: BranchExtras | null, status = pr({ pr_number: 8, ci: "failed" })) => {
    const { store } = showCards([branch(1, "devon/a", { status, agent: { label: "Open session", href: "claude://x" } })]);
    if (data) {
      act(() => store.getState().extrasChanged({ branches: [data], agents_decided_at: null, spend_read_at: null }));
    }
    return slotsOf("devon/a");
  };

  test("the session moved into the Agent group as the interactive session", () => {
    const found = show(extras());

    expect(found.session).toEqual(["Interactive session", "ok", "Open session"]);
  });

  test("a branch an agent may take says what it would do, with a button to run one", () => {
    const found = show(extras());

    expect(found.agent_status[2]).toBe("may fix / re-run ci now Run an agent now");
    expect(found.agent_run).toEqual(["Latest agent run", "na", "no run yet"]);
    expect(found.agent_run_summary.slice(1)).toEqual(["na", "–"]);
  });

  test("a held-back branch says why, and offers a run only when a person may override it", () => {
    const held = extras({
      trigger: "resolve_comments",
      reason: { code: "draft", text: "The PR is a draft: agents leave drafts to you.", until: null },
      can_override: true,
    });

    const found = show(held);

    expect(found.agent_status[2]).toContain("Resolve comments: The PR is a draft: agents leave drafts to you.");
    expect(screen.getByRole("button", { name: "Run an agent anyway" })).toBeInTheDocument();
  });

  test("a branch an agent is working on says so, with a stop button", () => {
    const working = extras({
      reason: { code: "working", text: "An agent is working on this now.", until: null },
      latest_run: {
        run_id: "r9",
        id: "1:devon/a",
        repo: "api",
        branch: "devon/a",
        trigger: "fix_ci",
        model: "claude-haiku-5-5",
        state: "running",
        started_at: new Date(NOW - 3 * 60_000).toISOString(),
        ended_at: null,
        turns: 3,
        cost_usd: 0.0123,
        summary: null,
        error: null,
      },
      runs: 1,
    });

    const found = show(working);

    expect(found.agent_status[2]).toBe("working Stop");
    expect(found.agent_run[2]).toBe("running Fix / re-run CI");
    expect(found.agent_run_summary[2]).toContain("still running");
  });

  test("the latest run shows its result and the cost of every run on the task", () => {
    const done = extras({
      has_action: false,
      trigger: null,
      latest_run: {
        run_id: "r1",
        id: "1:devon/a",
        repo: "api",
        branch: "devon/a",
        trigger: "resolve_comments",
        model: "claude-haiku-5-5",
        state: "succeeded",
        started_at: new Date(NOW - 3600_000).toISOString(),
        ended_at: new Date(NOW - 3000_000).toISOString(),
        turns: 9,
        cost_usd: 0.0831,
        summary: "Replied to two Copilot threads.",
        error: null,
      },
      runs: 2,
    });

    const found = show(done);

    expect(found.agent_status[2]).toBe("nothing for an agent to do");
    expect(found.agent_run).toEqual(["Latest agent run", "ok", "finished Resolve comments (2 runs in all)"]);
    expect(found.agent_run_summary[2]).toContain("Replied to two Copilot threads.");
    expect(found.cost).toEqual(["Cost", "ok", "$1.50"]);
    expect(found.agent_cost).toEqual(["Autonomous agent cost", "ok", "$0.0500 over 2 runs"]);
  });

  test("the Runs button opens a dialog to pick a run and read its cost and conversation", async () => {
    const said = { r2: "Replied to two Copilot threads.", r1: "Re-ran the flaky job." };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const runId = /runs\/(r\d)\/conversation/.exec(url)?.[1] as keyof typeof said;
        return {
          ok: true,
          status: 200,
          json: async () => ({
            run_id: runId,
            running: false,
            messages: [{ kind: "text", at: "t", text: said[runId], tool: null, is_error: false }],
          }),
        };
      }),
    );
    try {
      const run = (id: string, trigger: "fix_ci" | "resolve_comments", cost: number, turns: number, summary: string) => ({
        run_id: id,
        id: "1:devon/a",
        repo: "api",
        branch: "devon/a",
        trigger,
        model: "claude-haiku-5-5",
        state: "succeeded" as const,
        started_at: new Date(NOW - (id === "r2" ? 3600_000 : 7200_000)).toISOString(),
        ended_at: new Date(NOW - 3000_000).toISOString(),
        turns,
        cost_usd: cost,
        summary,
        error: null,
      });
      const latest = run("r2", "resolve_comments", 0.0831, 9, "Replied to two Copilot threads.");
      const earlier = run("r1", "fix_ci", 0.0213, 4, "Re-ran the flaky job.");
      const found = show(extras({ has_action: false, trigger: null, latest_run: latest, recent_runs: [latest, earlier], runs: 2 }));
      expect(found.agent_run_summary[2]).toContain("Replied to two Copilot threads.");

      act(() => screen.getByRole("button", { name: "Runs" }).click());

      const dialog = await screen.findByRole("dialog", { name: "Agent runs: devon/a" });
      // It floats over the page, not inside the card.
      expect(document.querySelector("article")?.contains(dialog)).toBe(false);
      const list = within(dialog).getByRole("list", { name: "Runs" });
      expect(within(list).getAllByRole("button")).toHaveLength(2);
      // The latest run is shown first, with its own fields and conversation.
      const detail = within(dialog).getByRole("region", { name: "Run" });
      expect(within(detail).getByText("$0.0831")).toBeInTheDocument();
      expect(within(detail).getByText("Resolve comments")).toBeInTheDocument();
      expect(await within(detail).findByText("Replied to two Copilot threads.", { selector: ".agent-message-text" })).toBeInTheDocument();

      act(() => within(list).getAllByRole("button")[1].click());

      expect(within(detail).getByText("$0.0213")).toBeInTheDocument();
      expect(within(detail).getByText("4")).toBeInTheDocument();
      expect(within(detail).getByText("Fix / re-run CI")).toBeInTheDocument();
      expect(await within(detail).findByText("Re-ran the flaky job.", { selector: ".agent-message-text" })).toBeInTheDocument();
      expect(within(list).getAllByRole("button")[1]).toHaveAttribute("aria-current", "true");

      act(() => within(dialog).getByRole("button", { name: "Close" }).click());
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  test("without extras the slots say they are loading", () => {
    const found = show(null);

    expect(found.agent_status.slice(1)).toEqual(["na", "loading agent info…"]);
    expect(found.cost.slice(1)).toEqual(["na", "loading spend…"]);
  });
});

describe("a card whose next action is Merge", () => {
  const ready = pr({
    pr_number: 8,
    review: "approved",
    next: { label: "Merge", who: "you", kind: "merge" },
  });
  const merge = (result: { ok: boolean; output: string } | Error, confirmed = true) => {
    const fetchMock = vi.fn(async (...args: [string, RequestInit?]) => {
      void args;
      if (result instanceof Error) return { ok: false, status: 409, json: async () => ({ detail: result.message }) };
      return { ok: true, status: 200, json: async () => result };
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => confirmed));
    showCards([branch(1, "devon/a", { status: ready })]);
    return fetchMock;
  };
  const mergePosts = (fetchMock: ReturnType<typeof merge>) =>
    fetchMock.mock.calls.filter(([url]) => url === "/api/merge");

  test("shows Merge as a button, not a pill", () => {
    merge({ ok: true, output: "" });

    const card = document.querySelector('article[data-id="1:devon/a"]') as HTMLElement;
    expect(within(card).getByRole("button", { name: "Merge" })).toBeInTheDocument();
    expect(card.querySelector('[data-slot="next"] .pill')).toBeNull();
  });

  test("other next actions stay pills", () => {
    showCards([branch(1, "devon/a", { status: pr({ pr_number: 8 }) })]);

    const card = document.querySelector('article[data-id="1:devon/a"]') as HTMLElement;
    expect(within(card).queryByRole("button", { name: "Merge" })).toBeNull();
    expect(card.querySelector('[data-slot="next"] .pill')).not.toBeNull();
  });

  test("pressing it asks first, then lands the PR", async () => {
    const fetchMock = merge({ ok: true, output: "merged" });

    await act(async () => {
      screen.getByRole("button", { name: "Merge" }).click();
    });

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("PR #8"));
    const [posted] = mergePosts(fetchMock);
    expect(posted[1]?.method).toBe("POST");
    expect(JSON.parse(String(posted[1]?.body))).toEqual({ id: "1:devon/a" });
  });

  test("declining the question lands nothing", async () => {
    const fetchMock = merge({ ok: true, output: "" }, false);

    await act(async () => {
      screen.getByRole("button", { name: "Merge" }).click();
    });

    expect(mergePosts(fetchMock)).toHaveLength(0);
  });

  test("a merge that fails shows toolbelt's output on the card", async () => {
    merge({ ok: false, output: "GraphQL: not mergeable" });

    await act(async () => {
      screen.getByRole("button", { name: "Merge" }).click();
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("GraphQL: not mergeable");
  });

  test("a refusal from the server is shown too", async () => {
    merge(new Error("Nothing to merge: the branch's next action is not Merge."));

    await act(async () => {
      screen.getByRole("button", { name: "Merge" }).click();
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("Nothing to merge");
  });
});


describe("a repo the user administers whose webhook is not working", () => {
  const badge = (id = "1:devon/a"): HTMLElement | null =>
    document.querySelector(`article[data-id="${id}"] .hook-problem`);

  test("is flagged on its cards, with why on hover, and the flag goes when it is fixed", () => {
    const { store } = showCards([branch(1, "devon/a", { status: pr({ pr_number: 8 }) })], { search: "?done=1" });
    expect(badge()).toBeNull();

    act(() =>
      store.getState().webhooksChanged([
        { repo_id: 1, state: "blocked", problem: "GitHub allows one forwarded webhook per repo, and this one is taken." },
      ]),
    );

    expect(badge()).toHaveTextContent("webhook blocked");
    expect(badge()).toHaveAttribute(
      "title",
      "You administer this repo, but its webhook is not working: GitHub allows one forwarded webhook per repo, and this one is taken.",
    );

    act(() => store.getState().webhooksChanged([]));

    expect(badge()).toBeNull();
  });

  test("says when the webhook is claimed by a forwarder outside toolbelt, and when the forwarder is just off", () => {
    const { store } = showCards([branch(1, "devon/a", { status: pr({ pr_number: 8 }) })], { search: "?done=1" });

    act(() =>
      store.getState().webhooksChanged([
        { repo_id: 1, state: "claimed", problem: "Another forwarder, not started by toolbelt, is connected to this repo's webhook." },
      ]),
    );
    expect(badge()).toHaveTextContent("webhook claimed elsewhere");
    expect(badge()).toHaveAttribute("title", expect.stringContaining("not started by toolbelt"));

    act(() => store.getState().webhooksChanged([{ repo_id: 1, state: "failed", problem: "Forwarding stopped: x" }]));
    expect(badge()).toHaveTextContent("webhook off");
  });

  test("is not flagged on the cards of another repo", () => {
    const { store } = showCards([branch(1, "devon/a", { status: pr({ pr_number: 8 }) })], { search: "?done=1" });

    act(() => store.getState().webhooksChanged([{ repo_id: 2, state: "failed", problem: "Forwarding stopped: x" }]));

    expect(badge()).toBeNull();
  });
});

describe("a card says how old its data is", () => {
  const fresh = (ageMs: number | null, status = pr({ pr_number: 8 })) => {
    const { store } = showCards([branch(1, "devon/a", { status })], { search: "?done=1" });
    store.getState().extrasChanged({
      branches: [],
      agents_decided_at: new Date(NOW - 3_000).toISOString(),
      spend_read_at: new Date(NOW - 45_000).toISOString(),
    });
    store
      .getState()
      .refreshed(1, ageMs === null ? [] : [{ branch_id: "1:devon/a", fetched_at: new Date(NOW - ageMs).toISOString(), error: null }]);
  };
  const pill = (group: string): HTMLElement =>
    document.querySelector(`article[data-id="1:devon/a"] .g-${group} .fresh`) as HTMLElement;

  test("each box counts the seconds since its own data was read", () => {
    act(() => fresh(40_000));

    expect(pill("pr")).toHaveTextContent("updated 40s ago");
    expect(pill("pr")).not.toHaveClass("stale");
    // The Agent box is as old as the agents' last decision, the Spend box as the
    // last read of the transcripts: not as old as the page's last request.
    expect(pill("agent")).toHaveTextContent("updated 3s ago");
    expect(pill("spend")).toHaveTextContent("updated 45s ago");
  });

  test("it goes amber when the lookup is overdue, and says so before the first one", () => {
    act(() => fresh(5 * 60_000));
    expect(pill("pr")).toHaveTextContent("updated 300s ago");
    expect(pill("pr")).toHaveClass("stale");
  });

  test("a failed lookup keeps the last good data, with its age and why it failed", () => {
    const { store } = showCards([branch(1, "devon/a", { status: pr({ pr_number: 8 }) })], { search: "?done=1" });
    act(() =>
      store.getState().refreshed(1, [
        {
          branch_id: "1:devon/a",
          fetched_at: new Date(NOW - 600_000).toISOString(),
          error: "GitHub graphql rate limit exceeded",
        },
      ]),
    );

    expect(pill("pr")).toHaveTextContent("updated 600s ago (GitHub graphql rate limit exceeded)");
    expect(pill("pr")).toHaveClass("stale");
    expect(pill("pr")).toHaveAttribute("title", expect.stringContaining("Latest attempt failed"));

    act(() =>
      store.getState().refreshed(2, [{ branch_id: "1:devon/a", fetched_at: new Date(NOW).toISOString(), error: null }]),
    );

    expect(pill("pr")).toHaveTextContent("updated 0s ago");
    expect(pill("pr")).not.toHaveClass("stale");
  });

  test("a branch that was never looked up says why", () => {
    const { store } = showCards([branch(1, "devon/a", { status: { load: "failed" } })], { search: "?done=1" });
    act(() =>
      store.getState().refreshed(1, [{ branch_id: "1:devon/a", fetched_at: null, error: "RuntimeError: gh missing" }]),
    );

    expect(pill("pr")).toHaveTextContent("not read yet (RuntimeError: gh missing)");
  });

  test("a PR that is merged is looked up rarely, so it is not overdue for minutes", () => {
    const merged = pr({ pr: "merged", pr_number: 3, merge: "none", next: { label: "Done", who: "none", kind: "done" } });

    act(() => fresh(5 * 60_000, merged));

    expect(pill("pr")).not.toHaveClass("stale");
  });

  test("before its first lookup the PR box says so", () => {
    act(() => fresh(null));

    expect(pill("pr")).toHaveTextContent("not read yet");
  });
});
