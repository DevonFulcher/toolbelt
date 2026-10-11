import { act, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { Branch, LoadedStatus } from "../api/generated";
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
  branch: "Repo / branch",
  next: "Next",
  pr: "PR",
  ci: "CI",
  review: "Review",
  comments: "Comments",
  merge: "Merge",
  task: "Task",
  session: "Session",
  stack: "Stack",
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
      merge: "conflicts",
      next: { label: "Resolve conflicts", who: "you", kind: "resolve_conflicts" },
      detail: detail({
        title: "Add <b>the</b> thing",
        pr_age: { text: "5d", since: new Date(NOW - 5 * DAY).toISOString() },
        review_request_age: { text: "1d", since: new Date(NOW - DAY).toISOString() },
        ci_failures: 2,
        reviewers: ["sam", "lee"],
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
    expect(b.comments).toEqual(["Comments", "ok", "3 comments"]);
    expect(b.merge[2]).toBe("conflicts");
    expect(b.pr_age[2]).toBe("5d");
    expect(b.in_draft).toEqual(["In draft", "na", "not a draft"]);
    expect(b.review_requested).toEqual(["Review requested", "ok", "1d"]);
    expect(b.ci_failures[2]).toBe("2 failures");
    expect(b.reviewers).toEqual(["Reviewers", "ok", "sam, lee"]);
    expect(b.stack[2]).toBe("devon/a stack 2 of 2 no children");
    // The title is text, never markup.
    expect(b.title[2]).toBe("Add <b>the</b> thing");
    expect(b.task[1]).toBe("gap");
    expect(b.task[2]).toBe("no task linked");

    expect(a.task).toEqual(["Task", "ok", "ABC-1"]);
    expect(a.session[1]).toBe("ok");
    expect(a.in_draft[2]).toBe("5d");
    expect(a.stack[2]).toBe("stack root stack 1 of 2 open in stacked view → 1 child");
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
    for (const key of ["ci", "review", "comments", "merge", "pr_age", "in_draft", "review_requested", "ci_failures", "reviewers", "title"]) {
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
    expect(found.session).toEqual(["Session", "gap", "no session linked"]);
    expect(found.stack[2]).toBe("stack root single branch no children");
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
    expect(found.comments).toEqual(["Comments", "na", "0 comments"]);
    expect(found.merge).toEqual(["Merge", "ok", "clean"]);
    expect(found.in_draft).toEqual(["In draft", "na", "not a draft"]);
    expect(found.review_requested).toEqual(["Review requested", "na", "not requested"]);
    expect(found.ci_failures).toEqual(["CI failures", "na", "0 failures"]);
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

  test("an unloaded card has every slot saying loading", () => {
    showCards([branch(1, "devon/a")]);

    const found = slotsOf("devon/a");

    expect(labels(found)).toEqual(SLOT_LABELS);
    for (const key of Object.keys(SLOT_LABELS)) {
      if (["branch", "task", "session", "stack", "task_age"].includes(key)) continue;
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
