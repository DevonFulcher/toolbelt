import { describe, expect, test } from "vitest";
import type { Chip, EventKind, TimelineEvent } from "../api/generated";
import { ago, catalog, DAY, event as ev, HOUR, MIN, NOW } from "../testing/fixtures";
import { formatAbsolute, formatAge, formatSpan } from "./format";
import {
  endTip,
  legendModel,
  listEntries,
  markerKind,
  nowChips,
  positionTip,
  tooltipContent,
  trackLayout,
  trackStart,
  type LayoutOptions,
  type Track,
} from "./timeline";

function track(createdAgoMs: number, events: TimelineEvent[] = [], extra: Partial<Track> = {}): Track & { created: string } {
  return {
    started: null,
    created: ago(createdAgoMs),
    ended: null,
    state: "open",
    draft: false,
    events: [ev("opened", createdAgoMs), ...events],
    current: [],
    ...extra,
  } as Track & { created: string };
}

const layout = (t: Track, options: Partial<LayoutOptions> = {}) =>
  trackLayout(t, { now: NOW, catalog, ...options });
const kinds = (l: ReturnType<typeof layout>): EventKind[] => l.markers.map((m) => m.kind);
const near = (actual: number, expected: number, eps = 1e-9) =>
  expect(Math.abs(actual - expected)).toBeLessThan(eps);

describe("track layout", () => {
  test("places markers proportionally between creation and now", () => {
    const l = layout(track(10 * DAY, [ev("review_requested", 5 * DAY, { subject: "sam" })]));

    near(l.markers.find((m) => m.kind === "review_requested")!.x, 0.5);
    near(l.markers.find((m) => m.kind === "opened")!.x, 0);
    expect(l.startLabel).toBe("started 10d ago");
    expect(l.endLabel).toBe("now");
    expect(l.now).toBe(1);
    expect(l.ended).toBe(false);
  });

  test("a merged PR's track ends at the merge, with no now marker", () => {
    const l = layout(track(10 * DAY, [ev("merged", 2 * DAY)], { state: "merged", ended: ago(2 * DAY) }));

    near(l.markers.find((m) => m.kind === "merged")!.x, 1);
    expect(l.endLabel).toBe("merged 2d ago");
    expect(l.startLabel).toBe("started 10d ago");
    expect(l.now).toBeNull();
    near(l.span, 8 * DAY);
  });

  test("a closed PR is labelled closed; a missing end event falls back to the last event", () => {
    const closed = layout(track(4 * DAY, [ev("closed", DAY)], { state: "closed", ended: ago(DAY) }));
    expect(closed.endLabel).toBe("closed 1d ago");

    // Merged, but the history we have does not include the merge event.
    const truncated = layout(track(4 * DAY, [ev("commit", 2 * DAY)], { state: "merged", ended: null }));
    expect(truncated.ended).toBe(true);
    near(truncated.span, 2 * DAY);
    expect(truncated.now).toBeNull();
  });

  test("a very short PR still lays out: minutes, and a zero-length span", () => {
    const short = layout(track(3 * MIN, [ev("review_requested", MIN, { subject: "sam" })]));
    expect(short.startLabel).toBe("started 3m ago");
    near(short.markers.find((m) => m.kind === "review_requested")!.x, 2 / 3);
    expect(short.ticks.every((tick) => tick.x > 0 && tick.x < 1)).toBe(true);

    // Created this very instant: the span is floored, nothing is NaN.
    const instant = layout(track(0, [ev("commit", 0)]), { commits: "all" });
    expect(instant.startLabel).toBe("started <1m ago");
    expect(instant.markers.every((m) => m.x >= 0 && m.x <= 1 && !Number.isNaN(m.x))).toBe(true);
  });

  test("a very long PR labels its age in weeks and keeps ticks off the ends", () => {
    const l = layout(track(45 * DAY, [ev("commit", 20 * DAY)]), { commits: "all" });

    expect(l.startLabel).toBe("started 6w ago");
    expect(l.ticks.length).toBeGreaterThanOrEqual(2);
    expect(l.ticks.length).toBeLessThanOrEqual(6);
    expect(l.ticks.every((tick) => tick.x > 0.06 && tick.x < 0.94)).toBe(true);
    const xs = l.ticks.map((tick) => tick.x);
    expect(xs).toEqual([...xs].sort((a, b) => a - b));
  });

  test("plain commits cluster when they would overlap and stay apart otherwise", () => {
    const burst: TimelineEvent[] = [];
    for (let i = 0; i < 50; i += 1) burst.push(ev("commit", 10 * DAY - i * MIN, { detail: `c${i}` }));
    const spread = [ev("commit", 20 * DAY), ev("commit", 2 * DAY)];
    const l = layout(track(30 * DAY, [...burst, ...spread]), { commits: "all" });

    const clusters = l.markers.filter((m) => m.kind === "commits");
    expect(clusters).toHaveLength(1);
    expect(clusters[0].count).toBe(50);
    expect(clusters[0].items).toHaveLength(50);
    near(clusters[0].x, 20 / 30, 0.001);
    expect(l.markers.filter((m) => m.kind === "commit")).toHaveLength(2);
  });

  test("ordinary commits are only drawn when asked for; failed ones are prominent", () => {
    const t = track(10 * DAY, [
      ev("commit", 8 * DAY, { ci: "success" }),
      ev("commit", 6 * DAY, { ci: "failed" }),
      ev("commit", 4 * DAY, { ci: "" }),
    ]);

    expect(kinds(layout(t))).toEqual(["opened", "commit_failed"]);
    expect(kinds(layout(t, { commits: "all" }))).toEqual(["opened", "commit", "commit_failed", "commit"]);
    // "none" hides commits entirely, failing ones too.
    expect(kinds(layout(t, { commits: "none" }))).toEqual(["opened"]);
  });

  test("review outcomes, force pushes and draft changes get their own kinds", () => {
    const t = track(6 * DAY, [
      ev("review", 5 * DAY, { detail: "approved", actor: "sam", subject: "sam" }),
      ev("review", 4 * DAY, { detail: "requested changes", actor: "lee", subject: "lee" }),
      ev("review", 3 * DAY, { detail: "commented", actor: "kim", subject: "kim" }),
      ev("force_push", 2 * DAY, { detail: "force-pushed a to b" }),
      ev("converted_to_draft", DAY),
      ev("ready_for_review", HOUR),
    ]);

    expect(kinds(layout(t, { laneGap: 0 }))).toEqual([
      "opened",
      "review_approved",
      "review_changes",
      "review_commented",
      "force_push",
      "converted_to_draft",
      "ready_for_review",
    ]);
  });

  test("overlapping prominent markers stack into lanes", () => {
    const same = [
      ev("review_requested", 2 * DAY, { subject: "sam" }),
      ev("review_requested", 2 * DAY, { subject: "lee" }),
      ev("ready_for_review", 2 * DAY),
    ];
    const l = layout(track(10 * DAY, same), { laneGap: 0.05 });

    expect(l.markers.filter((m) => m.kind !== "opened").map((m) => m.lane)).toEqual([0, 1, 2]);
    expect(l.lanes).toBe(3);
    // Far apart: one lane.
    const apart = layout(track(10 * DAY, [same[0], ev("merged", HOUR)]), { laneGap: 0.05 });
    expect(apart.lanes).toBe(1);
  });

  test("by pixel width, labels that would touch take other lanes, and drop out when no lane has room", () => {
    const t = track(10 * DAY, [
      ev("review_requested", 9.5 * DAY, { subject: "sam" }),
      ev("review", 9 * DAY, { actor: "lee", detail: "changes requested" }),
      ev("review", 8.5 * DAY, { actor: "sam", detail: "approved" }),
    ]);

    // Plenty of room: every label fits in the first lane.
    const wide = layout(t, { widthPx: 6000, maxLanes: 4 });
    expect(wide.markers.every((m) => m.lane === 0 && !m.labelHidden)).toBe(true);

    // A narrow track: the same events stack into lanes.
    const narrow = layout(t, { widthPx: 220, maxLanes: 4 });
    expect(new Set(narrow.markers.map((m) => m.lane)).size).toBeGreaterThan(1);

    // One lane only: what cannot fit is drawn as a bare marker, not on top of another label.
    const one = layout(t, { widthPx: 220, maxLanes: 1 });
    const shown = one.markers.filter((m) => !m.labelHidden);
    expect(shown.length).toBeLessThan(one.markers.length);
    expect(one.markers.filter((m) => !m.labelHidden).every((m) => m.lane === 0)).toBe(true);
  });

  test("a label flips left of its marker past the middle and carries its text", () => {
    const l = layout(track(10 * DAY, [ev("review", 2 * DAY, { actor: "sam", detail: "approved" })]), {
      widthPx: 400,
    });

    const review = l.markers.find((m) => m.kind === "review_approved")!;
    expect(review.flip).toBe(true);
    expect(review.label).toBe("approved · sam");
    const opened = l.markers.find((m) => m.kind === "opened")!;
    expect(opened.flip).toBe(false);
    expect(opened.label).toBe("opened");
  });

  test("events from before the PR was opened sit at its start", () => {
    const l = layout(track(2 * DAY, [ev("commit", 5 * DAY, { ci: "failed" })]));

    const early = l.markers.find((m) => m.kind === "commit_failed")!;
    expect(early.x).toBe(0);
    expect(early.title).toMatch(/5d ago/);
  });

  test("marker titles carry the kind, actor and age", () => {
    const l = layout(track(4 * DAY, [ev("review", 3 * HOUR, { detail: "approved", actor: "sam", subject: "sam" })]));

    expect(l.markers.find((m) => m.kind === "review_approved")!.title).toBe("approved (sam) 3h ago");
  });

  test("the plain list is chronological and honours the commit toggle", () => {
    const t = track(5 * DAY, [
      ev("review", DAY, { detail: "approved", actor: "sam", subject: "sam" }),
      ev("commit", 4 * DAY, { ci: "failed", detail: "abc fix" }),
    ]);

    const all = listEntries(t, { now: NOW, catalog, commits: "all" });
    expect(all.map((e) => e.label)).toEqual(["opened", "commit (CI failed)", "approved"]);
    expect(all[1].age).toBe("4d ago");
    const none = listEntries(t, { now: NOW, catalog, commits: "none" });
    expect(none.map((e) => e.kind)).toEqual(["opened", "review_approved"]);
  });
});

describe("phases", () => {
  test("draft and review-waiting phases are shaded as spans", () => {
    // Opened as a draft, ready after 2 days; a request made while still a
    // draft only starts waiting once it is ready; sam reviews on day 5.
    const t = track(10 * DAY, [
      ev("review_requested", 9 * DAY, { subject: "sam" }),
      ev("ready_for_review", 8 * DAY),
      ev("review", 5 * DAY, { detail: "approved", actor: "sam", subject: "sam" }),
    ]);

    const spans = layout(t).phases.map((p) => [
      p.kind,
      Math.round(p.x0 * 100) / 100,
      Math.round(p.x1 * 100) / 100,
    ]);

    expect(spans).toEqual([
      ["draft", 0, 0.2],
      ["waiting", 0.2, 0.5],
      ["approved", 0.5, 1],
    ]);
  });

  test("a PR that is a draft now and was never toggled is draft the whole way", () => {
    const l = layout(track(3 * DAY, [ev("commit", DAY)], { draft: true }));

    expect(l.phases.map((p) => [p.kind, p.x0, p.x1])).toEqual([["draft", 0, 1]]);
  });

  test("CI failing spans from a failed commit until the next commit", () => {
    const t = track(10 * DAY, [
      ev("commit", 8 * DAY, { ci: "failed" }),
      ev("commit", 6 * DAY, { ci: "success" }),
      ev("commit", 3 * DAY, { ci: "failed" }),
    ]);

    const failing = layout(t).phases.filter((p) => p.kind === "ci_failing");

    expect(failing).toHaveLength(2);
    near(failing[0].x0, 0.2);
    near(failing[0].x1, 0.4);
    // Still failing: runs to now.
    near(failing[1].x0, 0.7);
    near(failing[1].x1, 1);
  });

  test("a failed commit waits on CI until its checks completed, then fails until the next push", () => {
    const t = track(10 * DAY, [
      ev("commit", 8 * DAY, { ci: "failed", ci_done: ago(7 * DAY) }),
      ev("commit", 4 * DAY, { ci: "success", ci_done: ago(3 * DAY) }),
    ]);

    const phases = layout(t).phases.filter((p) => p.kind === "ci_running" || p.kind === "ci_failing");

    expect(phases.map((p) => [p.kind, Math.round(p.x0 * 100) / 100, Math.round(p.x1 * 100) / 100])).toEqual([
      ["ci_running", 0.2, 0.3],
      ["ci_failing", 0.3, 0.6],
      ["ci_running", 0.6, 0.7],
    ]);
  });

  test("a pending commit with no completion time is waiting on CI until now", () => {
    const l = layout(track(10 * DAY, [ev("commit", 2 * DAY, { ci: "pending" })]));

    const running = l.phases.filter((p) => p.kind === "ci_running");

    expect(running).toHaveLength(1);
    near(running[0].x0, 0.8);
    near(running[0].x1, 1);
  });

  test("a removed request or a review ends waiting; two reviewers need both", () => {
    const t = track(10 * DAY, [
      ev("review_requested", 8 * DAY, { subject: "sam" }),
      ev("review_requested", 7 * DAY, { subject: "lee" }),
      ev("review", 6 * DAY, { detail: "commented", actor: "sam", subject: "sam" }),
      ev("review_request_removed", 4 * DAY, { subject: "lee" }),
    ]);

    const waiting = layout(t).phases.filter((p) => p.kind === "waiting");

    expect(waiting).toHaveLength(1);
    near(waiting[0].x0, 0.2);
    near(waiting[0].x1, 0.6);
  });

  const approvedSpans = (t: Track & { created: string }) => layout(t).phases.filter((p) => p.kind === "approved");
  const approve = (agoMs: number) => ev("review", agoMs, { detail: "approved", actor: "sam", subject: "sam" });

  test("approved runs from the approving review to now", () => {
    const spans = approvedSpans(track(10 * DAY, [approve(4 * DAY)], { review: "approved" }));

    expect(spans).toHaveLength(1);
    near(spans[0].x0, 0.6);
    near(spans[0].x1, 1);
  });

  test("approved ends at the merge, and at a later request for changes", () => {
    const merged = track(10 * DAY, [approve(6 * DAY), ev("merged", 2 * DAY)], {
      state: "merged",
      ended: ago(2 * DAY),
    });
    near(approvedSpans(merged)[0].x1, 1);

    const changes = track(10 * DAY, [
      approve(6 * DAY),
      ev("review", 3 * DAY, { detail: "requested changes", actor: "sam", subject: "sam" }),
    ]);
    const spans = approvedSpans(changes);
    expect(spans).toHaveLength(1);
    near(spans[0].x0, 0.4);
    near(spans[0].x1, 0.7);
  });

  test("an approval that is stale now ends at the first push after it", () => {
    const events = [approve(6 * DAY), ev("commit", 4 * DAY, { ci: "success" })];

    const stale = approvedSpans(track(10 * DAY, events, { review: "none" }));
    expect(stale).toHaveLength(1);
    near(stale[0].x1, 0.6);

    // Still approved now: a later push does not end it.
    const still = approvedSpans(track(10 * DAY, events, { review: "approved" }));
    near(still[0].x1, 1);
  });
});

describe("now chips", () => {
  test("come from catalog references and are labelled by group", () => {
    const current: Chip[] = [
      { dimension: "pr", value: "open" },
      { dimension: "ci", value: "running" },
      { dimension: "comments", value: "open", n: 3 },
      { dimension: "merge", value: "conflicts" },
      { dimension: "next", value: "blocked", text: "Blocked on devon/a", who: "you" },
    ];

    const chips = nowChips({ current }, catalog);

    expect(chips.map((c) => [c.group, c.text, c.tone])).toEqual([
      ["PR", "open", "green"],
      ["CI", "running", "amber"],
      ["Comments", "3 unresolved", "amber"],
      ["Merge", "conflicts", "red"],
      ["Next", "Blocked on devon/a", "accent"],
    ]);
  });

  test("an unknown state or event kind is an error, not a blank", () => {
    const odd = { current: [{ dimension: "ci", value: "exploded" }] } as unknown as Track;
    expect(() => nowChips(odd, catalog)).toThrow(/no catalog entry for ci=exploded/);
    const teleported = track(DAY, [{ ...ev("opened", HOUR), kind: "teleported" } as unknown as TimelineEvent]);
    expect(() => layout(teleported)).toThrow(/no catalog entry for event kind "teleported"/);
  });
});

describe("time formatting", () => {
  test("spans read as the two largest units; absolute times as a local date", () => {
    expect(formatSpan(30)).toBe("<1m");
    expect(formatSpan(5 * 60)).toBe("5m");
    expect(formatSpan(3 * 3600 + 12 * 60)).toBe("3h 12m");
    expect(formatSpan(3600)).toBe("1h");
    expect(formatSpan(2 * 86400 + 4 * 3600 + 59)).toBe("2d 4h");
    expect(formatSpan(23 * 86400)).toBe("3w 2d");
    expect(formatAbsolute(Date.parse("2026-10-08T15:42:00Z"), { timeZone: "UTC" })).toBe("Oct 8, 2026, 3:42 PM");
    expect(formatAbsolute(Date.parse("2026-10-08T15:42:00Z"), { timeZone: "America/Chicago" })).toBe(
      "Oct 8, 2026, 10:42 AM",
    );
  });

  test("ages match the server's compact ages", () => {
    expect(formatAge(30)).toBe("<1m");
    expect(formatAge(300)).toBe("5m");
    expect(formatAge(3 * 3600)).toBe("3h");
    expect(formatAge(23 * 3600)).toBe("23h");
    expect(formatAge(86400)).toBe("1d");
    expect(formatAge(13 * 86400)).toBe("13d");
    expect(formatAge(14 * 86400)).toBe("2w");
    expect(formatAge(75 * 86400)).toBe("10w");
    expect(formatAge(-5)).toBe("<1m");
  });
});

describe("tooltips", () => {
  test("an event tooltip has kind, detail, actor, absolute and relative time", () => {
    const l = layout(
      track(4 * DAY, [ev("commit", 2 * DAY + 4 * HOUR, { detail: "abc1234 Fix it", ci: "failed", actor: "devon" })]),
    );
    const marker = l.markers.find((m) => m.kind === "commit_failed")!;

    const content = tooltipContent(marker.tip, NOW, { timeZone: "UTC" });

    expect(content.title).toBe("commit (CI failed)");
    expect(content.subtitle).toBe("abc1234 Fix it");
    expect(content.rows).toEqual([
      ["Actor", "devon"],
      ["CI", "failed"],
      ["When", "Oct 18, 2026, 8:00 AM"],
      ["Ago", "2d 4h ago"],
    ]);
  });

  test("a cluster tooltip lists its members and the range", () => {
    const burst: TimelineEvent[] = [];
    for (let i = 0; i < 12; i += 1) burst.push(ev("commit", 10 * DAY - i * MIN, { detail: `c${i}` }));
    const l = layout(track(30 * DAY, burst), { commits: "all" });
    const cluster = l.markers.find((m) => m.kind === "commits")!;

    const content = tooltipContent(cluster.tip, NOW, { timeZone: "UTC" });

    expect(content.title).toBe("12 commits");
    expect(content.rows.map(([k]) => k)).toEqual(["From", "To"]);
    expect(content.items).toHaveLength(9);
    expect(content.items[0]).toMatch(/devon · c0$/);
    expect(content.items[8]).toBe("+ 4 more");
  });

  test("a phase tooltip has its start, end and duration", () => {
    const t = track(10 * DAY, [ev("commit", 8 * DAY, { ci: "failed" }), ev("commit", 6 * DAY)]);
    const failing = layout(t).phases.find((p) => p.kind === "ci_failing")!;

    const content = tooltipContent(failing.tip, NOW, { timeZone: "UTC" });

    expect(content.title).toBe("CI failing");
    expect(content.rows.map(([k]) => k)).toEqual(["From", "To", "Duration"]);
    expect(content.rows[2][1]).toBe("2d");
    expect(content.rows[1][1]).toMatch(/\(6d ago\)$/);
    // A span running to now says so.
    const ongoing = layout(track(3 * DAY, [], { draft: true })).phases[0];
    expect(tooltipContent(ongoing.tip, NOW).rows[1][1]).toBe("now");
  });

  test("are placed below, flipped above at the bottom, clamped sideways", () => {
    const size = { width: 200, height: 100 };
    const viewport = { width: 800, height: 600 };
    const rect = (left: number, top: number) => ({ left, right: left + 10, top, bottom: top + 10 });

    const below = positionTip(rect(300, 100), size, viewport);
    expect(below.placement).toBe("below");
    expect(below.top).toBe(118);
    expect(below.left).toBe(205);

    const above = positionTip(rect(300, 560), size, viewport);
    expect(above.placement).toBe("above");
    expect(above.top).toBe(560 - 8 - 100);

    expect(positionTip(rect(0, 100), size, viewport).left).toBe(4);
    expect(positionTip(rect(795, 100), size, viewport).left).toBe(800 - 200 - 4);
  });
});

describe("catalog exhaustiveness: the renderer draws whatever the catalog says", () => {
  // The server event that produces each marker kind ("opened" is implicit).
  // A Record over the generated EventKind: a kind added on the server does
  // not compile here until the renderer maps an event to it.
  const SERVER_EVENTS: Record<Exclude<EventKind, "opened" | "started" | "commits">, Pick<TimelineEvent, "kind"> & Partial<TimelineEvent>> = {
    commit: { kind: "commit" },
    commit_failed: { kind: "commit", ci: "failed" },
    force_push: { kind: "force_push" },
    review_requested: { kind: "review_requested", subject: "sam" },
    review_request_removed: { kind: "review_request_removed", subject: "sam" },
    ready_for_review: { kind: "ready_for_review" },
    converted_to_draft: { kind: "converted_to_draft" },
    review_approved: { kind: "review", detail: "approved" },
    review_changes: { kind: "review", detail: "requested changes" },
    review_commented: { kind: "review", detail: "commented" },
    merged: { kind: "merged" },
    closed: { kind: "closed" },
    reopened: { kind: "reopened" },
  };

  test("every catalog event kind is produced by some server event, and every one is in the catalog", () => {
    // "opened" and "started" are not server events: the PR's creation and the
    // branch's own start time.
    const known = new Set<string>(["opened", "started", "commits", ...Object.keys(SERVER_EVENTS)]);
    expect(Object.keys(catalog.events).filter((k) => !known.has(k))).toEqual([]);
    expect([...known].filter((k) => !(k in catalog.events))).toEqual([]);

    for (const [kind, spec] of Object.entries(SERVER_EVENTS)) {
      expect(markerKind({ ci: "", detail: "", ...spec })).toBe(kind);
    }
  });

  test("every catalog state is representable: its kinds exist and it is listed in the end cap's tooltip", () => {
    for (const state of catalog.states) {
      expect(state.reps.length, `${state.dimension}=${state.value}`).toBeGreaterThan(0);
      for (const rep of state.reps) {
        const vocabulary: object = {
          event: catalog.events,
          phase: catalog.phases,
          now_state: catalog.chips,
        }[rep.how];
        expect(rep.kind in vocabulary, `${state.dimension}=${state.value}: ${rep.how} ${rep.kind}`).toBe(true);
      }
      if (state.dimension in catalog.chips) {
        const chip = { dimension: state.dimension, value: state.value, n: 2, text: "label", who: "you" } as Chip;
        const made = nowChips({ current: [chip] }, catalog);
        const tip = tooltipContent(endTip({ endKind: null, end: 0 }, made), 0);
        expect(tip.items, `${state.dimension}=${state.value}`).toEqual([
          `${made[0].group}: ${made[0].text}`,
        ]);
        expect(made[0].text && made[0].group).toBeTruthy();
      }
    }
  });

  test("every card slot has a dimension mapping in the catalog", () => {
    // A Record over the card's slot keys: the same list the Card renders.
    const slots = [
      "repo", "branch", "next", "pr", "ci", "review", "comments", "bot_comments", "merge", "conflict_commit", "task", "task_title", "task_status", "slack_status", "slack_age", "assigned_review", "session", "agent_status", "agent_runs", "agent_run", "agent_run_summary", "cost", "agent_cost",
      "ancestors", "descendants", "task_age", "estimate", "lines", "pr_age", "in_draft", "review_requested", "ci_failures", "title", "timeline",
    ];
    expect(Object.keys(catalog.slots).sort()).toEqual([...slots].sort());
    const dimensions = new Set(catalog.states.map((s) => s.dimension));
    for (const dimension of Object.values(catalog.slots)) {
      expect(dimension === null || dimensions.has(dimension)).toBe(true);
    }
  });

  test("the legend lists every catalog event, phase and chip value, and what is not shown", () => {
    const legend = legendModel(catalog);

    expect(legend.events.map((e) => e.kind)).toEqual(Object.keys(catalog.events));
    expect(legend.phases.map((p) => p.kind)).toEqual(Object.keys(catalog.phases));
    expect(legend.chips.map((c) => c.dimension)).toEqual(Object.keys(catalog.chips));
    for (const group of legend.chips) {
      const expected = catalog.states.filter((s) => s.dimension === group.dimension);
      expect(group.values, group.dimension).toHaveLength(expected.length);
    }
    expect(legend.notRepresented.some((n) => /conflict/i.test(n.label))).toBe(true);
    expect(legend.notRepresented.some((n) => /thread/i.test(n.label))).toBe(true);
  });
});

describe("a track that starts before the PR", () => {
  /** A task started 10d ago, its PR opened 4d ago. */
  const started = (extra: Partial<Track> = {}, events: TimelineEvent[] = []): Track =>
    ({ ...track(4 * DAY, events, extra), started: ago(10 * DAY) });

  test("begins at the task's start, with the PR opening part-way along", () => {
    const l = layout(started());

    expect(l.startLabel).toBe("started 10d ago");
    near(l.span, 10 * DAY);
    near(l.markers.find((m) => m.kind === "started")!.x, 0);
    near(l.markers.find((m) => m.kind === "opened")!.x, 0.6);
    expect(l.now).toBe(1);
  });

  test("shades a working phase from the start until the PR opened", () => {
    const working = layout(started()).phases.filter((p) => p.kind === "working");

    expect(working).toHaveLength(1);
    near(working[0].x0, 0);
    near(working[0].x1, 0.6);
    expect(working[0].label).toBe("Working (no PR yet)");
    expect(tooltipContent(working[0].tip, NOW).rows[2][1]).toBe("6d");
  });

  test("a draft PR's draft phase begins when the PR opened, not at the task's start", () => {
    const l = layout(started({ draft: true }));

    const draft = l.phases.find((p) => p.kind === "draft")!;
    near(draft.x0, 0.6);
    near(draft.x1, 1);
    expect(l.phases.map((p) => p.kind)).toEqual(["working", "draft"]);
  });

  test("commits made before the PR opened land inside the working phase", () => {
    const l = layout(started({}, [ev("commit", 8 * DAY), ev("commit", 7 * DAY), ev("commit", 2 * DAY)]), {
      commits: "all",
    });

    const commits = l.markers.filter((m) => m.kind === "commit");
    expect(commits.map((m) => m.x)).toHaveLength(3);
    near(commits[0].x, 0.2);
    near(commits[1].x, 0.3);
    near(commits[2].x, 0.8);
    const working = l.phases.find((p) => p.kind === "working")!;
    expect(commits.filter((m) => m.x > working.x0 && m.x < working.x1)).toHaveLength(2);
    // Clustering still groups the burst that happened before the PR.
    const burst: TimelineEvent[] = [];
    for (let i = 0; i < 20; i += 1) burst.push(ev("commit", 8 * DAY - i * MIN, { detail: `c${i}` }));
    const clustered = layout(started({}, burst), { commits: "all" }).markers.filter((m) => m.kind === "commits");
    expect(clustered).toHaveLength(1);
    expect(clustered[0].count).toBe(20);
    near(clustered[0].x, 0.2, 0.001);
    expect(tooltipContent(clustered[0].tip, NOW).items).toHaveLength(8 + 1);
  });

  test("a failed commit before the PR still draws as a prominent marker in the working phase", () => {
    const l = layout(started({}, [ev("commit", 8 * DAY, { ci: "failed" })]));

    const failed = l.markers.find((m) => m.kind === "commit_failed")!;
    near(failed.x, 0.2);
    // Its CI-failing phase belongs to the PR: it starts when the PR opened.
    near(l.phases.find((p) => p.kind === "ci_failing")!.x0, 0.6);
  });

  test("a merged PR's track still ends at the merge", () => {
    const l = layout(started({ state: "merged", ended: ago(DAY) }, [ev("merged", DAY)]));

    near(l.span, 9 * DAY);
    near(l.markers.find((m) => m.kind === "merged")!.x, 1);
    near(l.phases.find((p) => p.kind === "working")!.x1, 6 / 9);
    expect(l.now).toBeNull();
  });

  test("begins at the earliest of the start, the PR creation and the earliest event", () => {
    // A recorded start later than the PR (e.g. a branch imported after the
    // fact): the PR's opening begins the track, with no working phase.
    const late = layout({ ...track(4 * DAY), started: ago(2 * DAY) });
    expect(late.startLabel).toBe("started 4d ago");
    expect(late.phases.some((p) => p.kind === "working")).toBe(false);
    expect(kinds(late)).toEqual(["opened"]);

    // A commit older than both the start and the PR moves the start back.
    const early = layout({ ...track(4 * DAY, [ev("commit", 9 * DAY)]), started: ago(2 * DAY) }, { commits: "all" });
    expect(early.startLabel).toBe("started 9d ago");
    expect(kinds(early)).toEqual(["started", "commit", "opened"]);
    near(early.phases.find((p) => p.kind === "working")!.x1, 5 / 9);
  });

  test("no recorded start draws just the PR: no started marker, no working phase", () => {
    const l = layout(track(4 * DAY));

    expect(l.startLabel).toBe("started 4d ago");
    expect(kinds(l)).toEqual(["opened"]);
    expect(l.phases).toEqual([]);
  });
});

describe("a branch with no PR yet", () => {
  const noPr = (extra: Partial<Track> = {}): Track => ({
    started: ago(3 * DAY),
    created: null,
    ended: null,
    state: "none",
    draft: false,
    events: [],
    current: [],
    ...extra,
  });

  test("has a track from its start to now holding the working phase", () => {
    const l = layout(noPr());

    expect(l.startLabel).toBe("started 3d ago");
    expect(l.endLabel).toBe("now");
    expect(kinds(l)).toEqual(["started"]);
    expect(l.phases.map((p) => [p.kind, p.x0, p.x1])).toEqual([["working", 0, 1]]);
    expect(tooltipContent(l.phases[0].tip, NOW).rows[1][1]).toBe("now");
    expect(l.now).toBe(1);
  });

  test("has nothing to draw when the start is unknown too", () => {
    expect(trackStart(noPr({ started: null }))).toBeNull();
    expect(() => layout(noPr({ started: null }))).toThrow(/start or a PR creation/);
  });

  test("lists the task's start in the chronological list", () => {
    expect(listEntries(noPr(), { now: NOW, catalog }).map((e) => [e.label, e.age])).toEqual([
      ["Task started", "3d ago"],
    ]);
  });
});

describe("the chronological list with a task start", () => {
  test("puts the start first and the commits before the PR between it and the opening", () => {
    const entries = listEntries(
      {
        started: ago(10 * DAY),
        events: [ev("opened", 4 * DAY), ev("commit", 8 * DAY, { detail: "abc1234 first" })],
      },
      { now: NOW, catalog, commits: "all" },
    );

    expect(entries.map((e) => e.label)).toEqual(["Task started", "commit", "opened"]);
  });
});
