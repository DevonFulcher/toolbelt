import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { Branch, BranchClaim, BranchExtras, LoadedStatus, MergeQueueInfo, SlackInfo } from "../api/generated";
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
  task_title: "Task title",
  task_status: "Task status",
  slack_status: "Slack status",
  slack_age: "Post age",
  assigned_review: "Assigned Review",
  session: "Interactive session",
  agent_status: "Autonomous agent",
  agent_run: "Latest agent run",
  agent_runs: "Number of Autonomous Runs",
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
    for (const key of ["ci", "review", "comments", "bot_comments", "merge", "conflict_commit", "lines", "pr_age", "in_draft", "review_requested", "ci_failures", "title"]) {
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
        ["title", "pr", "ci", "review", "comments", "bot_comments", "merge", "conflict_commit", "lines", "pr_age", "in_draft", "review_requested", "ci_failures"],
        ["next", "task_age", "task", "task_title", "task_status", "slack_status", "slack_age", "assigned_review"],
        ["session", "agent_status", "agent_runs", "agent_run", "agent_run_summary"],
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
      if (["repo", "branch", "ancestors", "descendants", "task", "task_title", "task_status", "slack_status", "slack_age", "assigned_review", "session", "task_age", "agent_status", "agent_runs", "agent_run", "agent_run_summary", "cost", "agent_cost"].includes(key)) continue;
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

  test("the task, its title and its status are separate fields, or the title says why it is unavailable", () => {
    const withTask = (live: Branch["task_live"]) =>
      branch(1, "devon/a", {
        status: noPr(),
        task: { label: "ABC-1", href: "https://tasks.example/abc-1" },
        task_fetchable: true,
        task_live: live,
      });
    const { store } = showCards([withTask({ state: "loading" })]);
    expect(slotsOf("devon/a").task).toEqual(["Task", "ok", "ABC-1"]);
    expect(slotsOf("devon/a").task_title).toEqual(["Task title", "na", "loading…"]);
    expect(slotsOf("devon/a").task_status).toEqual(["Task status", "na", "loading…"]);

    act(() => {
      store.getState().taskChanged("1:devon/a", { state: "info", title: "Fix it", status: "In Progress" }, 1);
    });
    expect(slotsOf("devon/a").task).toEqual(["Task", "ok", "ABC-1"]);
    expect(slotsOf("devon/a").task_title).toEqual(["Task title", "ok", "Fix it"]);
    expect(slotsOf("devon/a").task_status).toEqual(["Task status", "ok", "In Progress"]);

    act(() => {
      store.getState().taskChanged("1:devon/a", { state: "unavailable", reason: "JIRA_API_TOKEN not set" }, 2);
    });
    expect(slotsOf("devon/a").task_title[2]).toBe("details unavailable ⚠");
    expect(screen.getByTitle("JIRA_API_TOKEN not set")).toBeInTheDocument();
    expect(slotsOf("devon/a").task_status).toEqual(["Task status", "na", "–"]);
    expect(screen.getByRole("link", { name: "ABC-1" })).toHaveAttribute("href", "https://tasks.example/abc-1");
  });

  test("a branch with no task has nothing in the title and status", () => {
    showCards([branch(1, "devon/a", { status: noPr() })]);

    expect(slotsOf("devon/a").task_title).toEqual(["Task title", "na", "–"]);
    expect(slotsOf("devon/a").task_status).toEqual(["Task status", "na", "–"]);
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

describe("a card whose next action is about Slack", () => {
  const next = (kind: "post_slack" | "bump_slack", label: string) =>
    pr({ pr_number: 8, pr: "open", next: { label, who: "you", kind, would_be: null } });

  test.each([
    ["post_slack", "Post in Slack"],
    ["bump_slack", "Bump in Slack"],
  ] as const)("%s is a button that opens the message, and posts nothing by itself", async (kind, label) => {
    const fetchMock = vi.fn(async (path: string) => ({
      ok: true,
      status: 200,
      json: async () => ({
        kind: kind === "post_slack" ? "post" : "bump",
        channel: path === "/api/slack/draft" ? "DTEST" : "?",
        text: "the message",
        assignee: null,
        candidates: [],
        prs: [],
        thread_ts: null,
        thread_link: null,
      }),
    }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      showCards([branch(1, "devon/a", { status: next(kind, label) })]);

      act(() => screen.getByRole("button", { name: label }).click());

      expect(await screen.findByRole("dialog", { name: label })).toBeInTheDocument();
      expect(await screen.findByDisplayValue("the message")).toBeInTheDocument();
      expect(fetchMock.mock.calls.map(([called]) => called)).toEqual(["/api/slack/draft"]);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("a card waiting on CI that can be queued to merge", () => {
  const waiting = pr({
    pr_number: 8,
    ci: "running",
    review: "approved",
    next: { label: "Waiting on CI", who: "ci", kind: "wait_ci", would_be: null },
  });
  const queueInfo = (over: Partial<MergeQueueInfo>): MergeQueueInfo => ({
    can_queue: false,
    queued: false,
    merging: false,
    queued_at: null,
    notice: null,
    ...over,
  });
  const answer = (result: { ok: boolean; status?: number; body?: unknown }) => {
    const fetchMock = vi.fn(async (...args: [string, RequestInit?]) => {
      void args;
      return { ok: result.ok, status: result.status ?? 200, json: async () => result.body ?? {} };
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  };

  test("offers to merge it when CI passes, and queues it when pressed", async () => {
    const fetchMock = answer({ ok: true, body: queueInfo({ queued: true }) });
    showCards([branch(1, "devon/a", { status: waiting, merge_queue: queueInfo({ can_queue: true }) })]);

    await act(async () => {
      screen.getByRole("button", { name: "Merge when CI passes" }).click();
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/merge/queue");
    expect(JSON.parse(String(init?.body))).toEqual({ id: "1:devon/a" });
  });

  test("a PR that cannot be queued offers nothing", () => {
    showCards([branch(1, "devon/a", { status: waiting, merge_queue: null })]);

    expect(screen.queryByRole("button", { name: "Merge when CI passes" })).toBeNull();
  });

  test("a queued PR says so and can be taken off the queue", async () => {
    const fetchMock = answer({ ok: true, status: 204 });
    showCards([branch(1, "devon/a", { status: waiting, merge_queue: queueInfo({ queued: true }) })]);

    expect(screen.getByText("Queued: merges when CI passes")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Merge when CI passes" })).toBeNull();
    await act(async () => {
      screen.getByRole("button", { name: "Cancel" }).click();
    });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/merge/unqueue");
  });

  test("a refusal from the server is shown", async () => {
    answer({ ok: false, status: 409, body: { detail: "Only a PR that is approved can be queued." } });
    showCards([branch(1, "devon/a", { status: waiting, merge_queue: queueInfo({ can_queue: true }) })]);

    await act(async () => {
      screen.getByRole("button", { name: "Merge when CI passes" }).click();
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("Only a PR that is approved can be queued.");
  });

  test("why a PR was taken off the queue is shown on its card", () => {
    const failed = pr({ pr_number: 8, ci: "failed", next: { label: "Fix / re-run CI", who: "you", kind: "fix_ci", would_be: null } });
    showCards([
      branch(1, "devon/a", { status: failed, merge_queue: queueInfo({ notice: "Taken off the merge queue: its CI failed." }) }),
    ]);

    expect(screen.getByRole("alert")).toHaveTextContent("Taken off the merge queue: its CI failed.");
  });

  test("once its CI has passed a queued PR reads Merging, not a Merge button", () => {
    const ready = pr({ pr_number: 8, review: "approved", next: { label: "Merge", who: "you", kind: "merge" } });
    showCards([branch(1, "devon/a", { status: ready, merge_queue: queueInfo({ queued: true, merging: true }) })]);

    expect(screen.getByText("Merging…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Merge" })).toBeNull();
  });

  test("a status event with the queue standing updates the card", () => {
    const { store } = showCards([branch(1, "devon/a", { status: waiting, merge_queue: null })]);

    act(() => store.getState().statusChanged("1:devon/a", waiting, 1, undefined, queueInfo({ can_queue: true })));
    expect(screen.getByRole("button", { name: "Merge when CI passes" })).toBeInTheDocument();

    act(() => store.getState().statusChanged("1:devon/a", waiting, 2, undefined, null));
    expect(screen.queryByRole("button", { name: "Merge when CI passes" })).toBeNull();
  });
});

describe("a card while a message is posted in the background", () => {
  const next = pr({ pr_number: 8, pr: "open", next: { label: "Post in Slack", who: "you", kind: "post_slack", would_be: null } });
  const slackInfo = (over: Partial<SlackInfo>): SlackInfo => ({
    posted: false,
    last_kind: null,
    last_at: null,
    bump_due: false,
    posting: false,
    notice: null,
    thread_link: null,
    assignee: null,
    ...over,
  });

  test("the button is disabled and says it is posting, and the status says so too", () => {
    showCards([branch(1, "devon/a", { status: next, slack: slackInfo({ posting: true }) })]);

    expect(screen.getByRole("button", { name: "Posting in Slack…" })).toBeDisabled();
    expect(slotsOf("devon/a").slack_status[2]).toBe("Posting in Slack…");
  });

  test("a failed post says why on the card and the button is there to try again", () => {
    showCards([branch(1, "devon/a", { status: next, slack: slackInfo({ notice: "Posting in Slack failed: timed out" }) })]);

    expect(screen.getByRole("button", { name: "Post in Slack" })).toBeEnabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Posting in Slack failed: timed out");
  });

  test("a posted PR's status links to its thread", () => {
    showCards([
      branch(1, "devon/a", {
        status: pr({ pr_number: 8, pr: "open" }),
        slack: slackInfo({
          posted: true,
          last_kind: "post",
          last_at: new Date(NOW - 3600_000).toISOString(),
          thread_link: "https://s.slack.com/archives/C1/p1",
        }),
      }),
    ]);

    expect(screen.getByRole("link", { name: "Posted in Slack" })).toHaveAttribute(
      "href",
      "https://s.slack.com/archives/C1/p1",
    );
  });
});

describe("a card with a question for the user", () => {
  test("its Next says Deciding, as a button that opens the questions", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ decisions: [] }) })),
    );
    const deciding = pr({
      pr_number: 8,
      pr: "open",
      next: {
        label: "Deciding",
        who: "you",
        kind: "deciding",
        would_be: { label: "Fix / re-run CI", who: "you", kind: "fix_ci" },
      },
    });
    showCards([branch(1, "devon/a", { status: deciding })]);

    expect(slotsOf("devon/a").next[2]).toContain("would be: Fix / re-run CI");
    fireEvent.click(screen.getByRole("button", { name: "Deciding" }));

    expect(await screen.findByRole("dialog", { name: "Decisions" })).toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe("/api/decisions?id=1%3Adevon%2Fa");
    vi.unstubAllGlobals();
  });
});

describe("a card's Refresh button", () => {
  test("each card has one, and it refreshes only that card's PR", async () => {
    const sent: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        sent.push(JSON.parse(String(init.body)));
        return { ok: true, status: 202, json: async () => ({ refreshing: true }) };
      }),
    );
    const open = pr({ pr_number: 8, pr: "open" });
    showCards([branch(1, "devon/a", { status: open }), branch(1, "devon/b", { status: open })]);

    const buttons = screen.getAllByRole("button", { name: "Refresh" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[1]);

    await waitFor(() => expect(sent).toEqual([{ id: "1:devon/b" }]));
    vi.unstubAllGlobals();
  });
});

describe("a card's Assigned Review", () => {
  const open = pr({ pr_number: 8, pr: "open" });
  const info = (assignee: string | null): SlackInfo => ({
    posted: assignee !== null,
    last_kind: assignee ? "post" : null,
    last_at: assignee ? new Date(NOW - 3600_000).toISOString() : null,
    bump_due: false,
    posting: false,
    notice: null,
    thread_link: null,
    assignee,
  });

  test("names who the latest Slack post or bump asked to review it", () => {
    showCards([branch(1, "devon/a", { status: open, slack: info("will.james") })]);

    expect(slotsOf("devon/a").assigned_review).toEqual(["Assigned Review", "ok", "will.james Reassign"]);
  });

  test("says nobody is assigned until it is posted", () => {
    showCards([branch(1, "devon/a", { status: open, slack: info(null) })]);

    expect(slotsOf("devon/a").assigned_review).toEqual(["Assigned Review", "na", "not assigned"]);
  });

  test("has nothing to say when Slack is off", () => {
    showCards([branch(1, "devon/a", { status: open, slack: null })]);

    expect(slotsOf("devon/a").assigned_review).toEqual(["Assigned Review", "na", "–"]);
  });

  test("follows a bump to the person asked then", () => {
    const { store } = showCards([branch(1, "devon/a", { status: open, slack: info("patrick.yost") })]);

    act(() => store.getState().statusChanged("1:devon/a", open, 1, info("will.james")));

    expect(slotsOf("devon/a").assigned_review[2]).toBe("will.james Reassign");
  });

  describe("Reassign", () => {
    const reassign = () => screen.queryByRole("button", { name: "Reassign" });

    test("a posted open PR can be reassigned, before a bump is due", () => {
      showCards([branch(1, "devon/a", { status: open, slack: info("will.james") })]);

      expect(reassign()).toBeInTheDocument();
    });

    test("a draft PR can be reassigned too", () => {
      showCards([branch(1, "devon/a", { status: pr({ pr_number: 8, pr: "draft" }), slack: info("will.james") })]);

      expect(reassign()).toBeInTheDocument();
    });

    test("a PR that is not posted, being posted, or no longer open has nothing to reassign", () => {
      showCards([
        branch(1, "devon/a", { status: open, slack: info(null) }),
        branch(1, "devon/b", { status: open, slack: { ...info("will.james"), posting: true } }),
        branch(1, "devon/c", { status: pr({ pr_number: 9, pr: "merged" }), slack: info("will.james") }),
      ]);

      expect(reassign()).toBeNull();
    });

    test("it opens the reassignment message to confirm", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: "nothing here" }) })),
      );
      showCards([branch(1, "devon/a", { status: open, slack: info("will.james") })]);

      fireEvent.click(screen.getByRole("button", { name: "Reassign" }));

      expect(await screen.findByRole("alert")).toHaveTextContent("nothing here");
      expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body))).toEqual({
        id: "1:devon/a",
        assignee_id: null,
        reassign: true,
      });
      vi.unstubAllGlobals();
    });
  });

  test("the old Reviewers slot is gone", () => {
    showCards([branch(1, "devon/a", { status: open, slack: info("will.james") })]);

    expect(slotsOf("devon/a").reviewers).toBeUndefined();
  });
});

describe("a card's Slack fields", () => {
  const open = pr({ pr_number: 8, pr: "open" });

  test("Slack off, or no PR yet: dashes", () => {
    showCards([branch(1, "devon/a", { status: open, slack: null })]);

    const found = slotsOf("devon/a");

    expect(found.slack_status).toEqual(["Slack status", "na", "–"]);
    expect(found.slack_age).toEqual(["Post age", "na", "–"]);
  });

  test("a ready PR not posted is flagged as something to do", () => {
    showCards([
      branch(1, "devon/a", { status: open, slack: { posted: false, last_kind: null, last_at: null, bump_due: false, posting: false, notice: null, thread_link: null, assignee: null } }),
    ]);

    const found = slotsOf("devon/a");

    expect(found.slack_status).toEqual(["Slack status", "gap", "Not posted in Slack"]);
    expect(found.slack_age[2]).toBe("–");
  });

  test("a draft not posted is not flagged: it is not ready", () => {
    const draft = pr({ pr_number: 8, pr: "draft" });
    showCards([
      branch(1, "devon/a", { status: draft, slack: { posted: false, last_kind: null, last_at: null, bump_due: false, posting: false, notice: null, thread_link: null, assignee: null } }),
    ]);

    expect(slotsOf("devon/a").slack_status).toEqual(["Slack status", "na", "Not posted in Slack"]);
  });

  test("a posted PR says so, and how long ago it was last posted or bumped", () => {
    showCards([
      branch(1, "devon/a", {
        status: open,
        slack: { posted: true, last_kind: "post", last_at: new Date(NOW - 5 * 3600_000).toISOString(), bump_due: false, posting: false, notice: null, thread_link: null, assignee: null },
      }),
      branch(1, "devon/b", {
        status: open,
        slack: { posted: true, last_kind: "bump", last_at: new Date(NOW - 2 * DAY).toISOString(), bump_due: true, posting: false, notice: null, thread_link: null, assignee: null },
      }),
    ]);

    expect(slotsOf("devon/a").slack_status).toEqual(["Slack status", "ok", "Posted in Slack"]);
    expect(slotsOf("devon/a").slack_age).toEqual(["Post age", "ok", "5h"]);
    expect(slotsOf("devon/b").slack_status).toEqual(["Slack status", "ok", "Bumped in Slack"]);
    expect(slotsOf("devon/b").slack_age[2]).toBe("2d");
  });

  test("a status event with the Slack standing updates the card", () => {
    const { store } = showCards([branch(1, "devon/a", { status: open, slack: null })]);

    act(() =>
      store.getState().statusChanged("1:devon/a", open, 1, {
        posted: true,
        last_kind: "post",
        last_at: new Date(NOW - 3600_000).toISOString(),
        bump_due: false,
        posting: false,
        notice: null,
        thread_link: null,
        assignee: null,
      }),
    );

    expect(slotsOf("devon/a").slack_status[2]).toBe("Posted in Slack");

    act(() => store.getState().statusChanged("1:devon/a", open, 2, null));

    expect(slotsOf("devon/a").slack_status[2]).toBe("–");
  });

  test("a status event without a Slack field leaves it as it was", () => {
    const { store } = showCards([
      branch(1, "devon/a", {
        status: open,
        slack: { posted: true, last_kind: "post", last_at: new Date(NOW - 3600_000).toISOString(), bump_due: false, posting: false, notice: null, thread_link: null, assignee: null },
      }),
    ]);

    act(() => store.getState().statusChanged("1:devon/a", open, 1));

    expect(slotsOf("devon/a").slack_status[2]).toBe("Posted in Slack");
  });
});

describe("a card's agent and spend slots", () => {
  const extras = (over: Partial<BranchExtras["agent"]> = {}, spend: Partial<BranchExtras["spend"]> = {}): BranchExtras => ({
    id: "1:devon/a",
    agent: {
      has_action: true,
      trigger: "fix_ci",
      reason: null,
      can_override: false, starting: false, start_error: null,
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
    expect(found.agent_runs.slice(1)).toEqual(["na", "0"]);
  });

  test("a held-back branch says why, and offers a run only when a person may override it", () => {
    const held = extras({
      trigger: "resolve_comments",
      reason: { code: "draft", text: "The PR is a draft: agents leave drafts to you.", until: null },
      can_override: true, starting: false, start_error: null,
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
        turns: 3, activity: null,
        cost_usd: 0.0123,
        summary: null,
        error: null,
      },
      runs: 1,
    });

    const found = show(working);

    expect(found.agent_status[2]).toBe("working Stop");
    expect(found.agent_run[2]).toBe("running Fix / re-run CI for 3m, turn 3, $0.0123 so far");
    expect(found.agent_run_summary[2]).toContain("starting up");
  });

  const workingRun = (activity: string | null) =>
    extras({
      reason: { code: "working", text: "An agent is working on this now.", until: null },
      latest_run: {
        run_id: "r9",
        id: "1:devon/a",
        repo: "api",
        branch: "devon/a",
        trigger: "fix_ci",
        model: "claude-haiku-5-5",
        state: "running",
        started_at: new Date(NOW - 90_000).toISOString(),
        ended_at: null,
        turns: 7,
        activity,
        cost_usd: 0.05,
        summary: null,
        error: null,
      },
      runs: 1,
    });

  test("a run that is still working shows what it is doing now", () => {
    const found = show(workingRun("Running: gh run view 123"));

    expect(found.agent_run_summary[2]).toContain("Now: Running: gh run view 123");
    expect(found.agent_run[2]).toContain("turn 7");
  });

  test("a run asked for by hand reads starting, not a button, until it has started", () => {
    const found = show(extras({ starting: true, reason: null, trigger: "fix_ci" }));

    expect(found.agent_status[2]).toContain("starting…");
    expect(screen.queryByRole("button", { name: /Run an agent/ })).toBeNull();
  });

  test("the run button says it is starting while the request is on its way", async () => {
    let release: () => void = () => undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        await new Promise<void>((resolve) => (release = resolve));
        return { ok: true, status: 202, json: async () => ({ starting: true }) };
      }),
    );
    show(extras({ reason: null, trigger: "fix_ci" }));

    act(() => screen.getByRole("button", { name: "Run an agent now" }).click());

    expect(await screen.findByRole("button", { name: "Starting…" })).toBeDisabled();
    release();
    vi.unstubAllGlobals();
  });

  test("why a run asked for by hand did not start is shown on the card", () => {
    const found = show(
      extras({
        reason: { code: "claimed", text: "A person or an agent session holds this task.", until: null },
        can_override: false,
        start_error: "A person or an agent session holds this task.",
      }),
    );

    expect(found.agent_status[2]).toContain("holds this task");
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
        turns: 9, activity: null,
        cost_usd: 0.0831,
        summary: "Replied to two Copilot threads.",
        error: null,
      },
      runs: 2,
    });

    const found = show(done);

    expect(found.agent_status[2]).toBe("nothing for an agent to do");
    expect(found.agent_runs).toEqual(["Number of Autonomous Runs", "ok", "2"]);
    expect(found.agent_run).toEqual(["Latest agent run", "ok", "finished Resolve comments"]);
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
        activity: null,
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
  const merge = (result: { merging: boolean } | Error) => {
    const fetchMock = vi.fn(async (...args: [string, RequestInit?]) => {
      void args;
      if (result instanceof Error) return { ok: false, status: 409, json: async () => ({ detail: result.message }) };
      return { ok: true, status: 202, json: async () => result };
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    showCards([branch(1, "devon/a", { status: ready })]);
    return { fetchMock, confirm };
  };
  const mergePosts = (fetchMock: ReturnType<typeof merge>["fetchMock"]) =>
    fetchMock.mock.calls.filter(([url]) => url === "/api/merge");
  const openDialog = async () => {
    await act(async () => {
      screen.getByRole("button", { name: "Merge" }).click();
    });
    return screen.findByRole("dialog", { name: "Merge PR #8" });
  };

  test("shows Merge as a button, not a pill", () => {
    merge({ merging: true });

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

  test("pressing it opens a dialog, not the browser's confirm box, and merges nothing yet", async () => {
    const { fetchMock, confirm } = merge({ merging: true });

    const dialog = await openDialog();

    expect(within(dialog).getByText(/Squash-merge PR #8/)).toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();
    expect(mergePosts(fetchMock)).toHaveLength(0);
  });

  test("confirming in the dialog lands the PR and closes it", async () => {
    const { fetchMock } = merge({ merging: true });
    const dialog = await openDialog();

    await act(async () => {
      within(dialog).getByRole("button", { name: "Merge" }).click();
    });

    const [posted] = mergePosts(fetchMock);
    expect(posted[1]?.method).toBe("POST");
    expect(JSON.parse(String(posted[1]?.body))).toEqual({ id: "1:devon/a" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("cancelling lands nothing and closes the dialog", async () => {
    const { fetchMock } = merge({ merging: true });
    const dialog = await openDialog();

    await act(async () => {
      within(dialog).getByRole("button", { name: "Cancel" }).click();
    });

    expect(mergePosts(fetchMock)).toHaveLength(0);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("a merge that fails in the background leaves toolbelt's output on the card", () => {
    merge({ merging: true });
    const failed = { can_queue: false, queued: false, merging: false, queued_at: null, notice: "The merge failed: GraphQL: not mergeable" };
    showCards([branch(1, "devon/f", { status: ready, merge_queue: failed })]);

    expect(screen.getAllByRole("alert").some((el) => el.textContent?.includes("The merge failed: GraphQL: not mergeable"))).toBe(true);
  });

  test("a merge in progress reads Merging, with no button to press twice", () => {
    merge({ merging: true });
    const merging = { can_queue: false, queued: false, merging: true, queued_at: null, notice: null };
    showCards([branch(1, "devon/b", { status: ready, merge_queue: merging })]);

    expect(screen.getAllByText("Merging…").length).toBeGreaterThan(0);
    const card = document.querySelector('article[data-id="1:devon/b"]') as HTMLElement;
    expect(within(card).queryByRole("button", { name: "Merge" })).toBeNull();
  });

  test("a refusal from the server is shown too", async () => {
    merge(new Error("Nothing to merge: the branch's next action is not Merge."));
    const dialog = await openDialog();

    await act(async () => {
      within(dialog).getByRole("button", { name: "Merge" }).click();
    });

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Nothing to merge");
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
