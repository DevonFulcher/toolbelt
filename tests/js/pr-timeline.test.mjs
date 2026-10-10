// Unit tests for the pure half of src/toolbelt/ui/static/pr-timeline.js:
// where a PR's events, phases and clusters sit along its per-card track.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const tl = require("../../src/toolbelt/ui/static/pr-timeline.js");

const NOW = Date.parse("2026-10-20T12:00:00Z");
const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const iso = (ms) => new Date(ms).toISOString();
const ago = (ms) => iso(NOW - ms);

function ev(kind, agoMs, extra = {}) {
  return { t: ago(agoMs), kind, actor: "devon", detail: "", ci: "", subject: "", ...extra };
}

function track(createdAgoMs, events = [], extra = {}) {
  return {
    created: ago(createdAgoMs),
    ended: null,
    state: "open",
    draft: false,
    events: [ev("opened", createdAgoMs), ...events],
    ...extra,
  };
}

const layout = (t, options = {}) => tl.trackLayout(t, { now: NOW, ...options });
const kinds = (l) => l.markers.map((m) => m.kind);
const near = (actual, expected, eps = 1e-9) =>
  assert.ok(Math.abs(actual - expected) < eps, `${actual} !~ ${expected}`);

test("markers are placed proportionally between creation and now", () => {
  const l = layout(track(10 * DAY, [ev("review_requested", 5 * DAY, { subject: "sam" })]));

  const requested = l.markers.find((m) => m.kind === "review_requested");
  near(requested.x, 0.5);
  near(l.markers.find((m) => m.kind === "opened").x, 0);
  assert.equal(l.startLabel, "opened 10d ago");
  assert.equal(l.endLabel, "now");
  assert.equal(l.now, 1);
  assert.equal(l.ended, false);
});

test("a merged PR's track ends at the merge, with no now marker", () => {
  const t = track(10 * DAY, [ev("merged", 2 * DAY)], { state: "merged", ended: ago(2 * DAY) });

  const l = layout(t);

  near(l.markers.find((m) => m.kind === "merged").x, 1);
  assert.equal(l.endLabel, "merged 2d ago");
  assert.equal(l.startLabel, "opened 10d ago");
  assert.equal(l.now, null);
  near(l.span, 8 * DAY);
});

test("a closed PR is labelled closed; a missing end event falls back to the last event", () => {
  const closed = layout(
    track(4 * DAY, [ev("closed", DAY)], { state: "closed", ended: ago(DAY) }),
  );
  assert.equal(closed.endLabel, "closed 1d ago");

  // Merged, but the history we have does not include the merge event.
  const truncated = layout(
    track(4 * DAY, [ev("commit", 2 * DAY)], { state: "merged", ended: null }),
  );
  assert.equal(truncated.ended, true);
  near(truncated.span, 2 * DAY);
  assert.equal(truncated.now, null);
});

test("a very short PR still lays out: minutes, and a zero-length span", () => {
  const short = layout(track(3 * MIN, [ev("review_requested", 1 * MIN, { subject: "sam" })]));
  assert.equal(short.startLabel, "opened 3m ago");
  near(short.markers.find((m) => m.kind === "review_requested").x, 2 / 3);
  assert.ok(short.ticks.every((tick) => tick.x > 0 && tick.x < 1));

  // Created this very instant: the span is floored, nothing is NaN.
  const instant = layout(track(0, [ev("commit", 0)]), { commits: "all" });
  assert.equal(instant.startLabel, "opened <1m ago");
  assert.ok(instant.markers.every((m) => m.x >= 0 && m.x <= 1 && !Number.isNaN(m.x)));
});

test("a very long PR labels its age in weeks and keeps ticks off the ends", () => {
  const l = layout(track(45 * DAY, [ev("commit", 20 * DAY)]), { commits: "all" });

  assert.equal(l.startLabel, "opened 6w ago");
  assert.ok(l.ticks.length >= 2 && l.ticks.length <= 6);
  assert.ok(l.ticks.every((tick) => tick.x > 0.06 && tick.x < 0.94));
  // Sorted left to right.
  assert.deepEqual(
    l.ticks.map((tick) => tick.x),
    [...l.ticks.map((tick) => tick.x)].sort((a, b) => a - b),
  );
});

test("plain commits cluster when they would overlap and stay apart otherwise", () => {
  const burst = [];
  for (let i = 0; i < 50; i += 1) burst.push(ev("commit", 10 * DAY - i * MIN, { detail: `c${i}` }));
  const spread = [ev("commit", 20 * DAY), ev("commit", 2 * DAY)];
  const l = layout(track(30 * DAY, [...burst, ...spread]), { commits: "all" });

  const clusters = l.markers.filter((m) => m.kind === "commits");
  assert.equal(clusters.length, 1);
  assert.equal(clusters[0].count, 50);
  assert.equal(clusters[0].items.length, 50);
  near(clusters[0].x, 20 / 30, 0.001);
  assert.equal(l.markers.filter((m) => m.kind === "commit").length, 2);
});

test("ordinary commits are only drawn when asked for; failed ones are prominent", () => {
  const t = track(10 * DAY, [
    ev("commit", 8 * DAY, { ci: "success" }),
    ev("commit", 6 * DAY, { ci: "failed" }),
    ev("commit", 4 * DAY, { ci: "" }),
  ]);

  assert.deepEqual(kinds(layout(t)), ["opened", "commit_failed"]);
  assert.deepEqual(kinds(layout(t, { commits: "all" })), [
    "opened",
    "commit",
    "commit_failed",
    "commit",
  ]);
  // "none" hides commits entirely, failing ones too.
  assert.deepEqual(kinds(layout(t, { commits: "none" })), ["opened"]);
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

  assert.deepEqual(kinds(layout(t, { laneGap: 0 })), [
    "opened",
    "review_approved",
    "review_changes",
    "review_commented",
    "force_push",
    "converted_to_draft",
    "ready_for_review",
  ]);
});

test("draft and review-waiting phases are shaded as spans", () => {
  // Opened as a draft, ready after 2 days; a request made while still a
  // draft only starts waiting once it is ready; sam reviews on day 5.
  const t = track(
    10 * DAY,
    [
      ev("review_requested", 9 * DAY, { subject: "sam" }),
      ev("ready_for_review", 8 * DAY),
      ev("review", 5 * DAY, { detail: "approved", actor: "sam", subject: "sam" }),
    ],
    { draft: false },
  );

  const phases = layout(t).phases;

  assert.deepEqual(
    phases.map((p) => [p.kind, Math.round(p.x0 * 100) / 100, Math.round(p.x1 * 100) / 100]),
    [
      ["draft", 0, 0.2],
      ["waiting", 0.2, 0.5],
    ],
  );
});

test("a PR that is a draft now and was never toggled is draft the whole way", () => {
  const l = layout(track(3 * DAY, [ev("commit", DAY)], { draft: true }));

  assert.deepEqual(
    l.phases.map((p) => [p.kind, p.x0, p.x1]),
    [["draft", 0, 1]],
  );
});

test("CI failing spans from a failed commit until the next commit", () => {
  const t = track(10 * DAY, [
    ev("commit", 8 * DAY, { ci: "failed" }),
    ev("commit", 6 * DAY, { ci: "success" }),
    ev("commit", 3 * DAY, { ci: "failed" }),
  ]);

  const failing = layout(t).phases.filter((p) => p.kind === "ci_failing");

  assert.equal(failing.length, 2);
  near(failing[0].x0, 0.2);
  near(failing[0].x1, 0.4);
  // Still failing: runs to now.
  near(failing[1].x0, 0.7);
  near(failing[1].x1, 1);
});

test("a removed request or a review ends waiting; two reviewers need both", () => {
  const t = track(10 * DAY, [
    ev("review_requested", 8 * DAY, { subject: "sam" }),
    ev("review_requested", 7 * DAY, { subject: "lee" }),
    ev("review", 6 * DAY, { detail: "commented", actor: "sam", subject: "sam" }),
    ev("review_request_removed", 4 * DAY, { subject: "lee" }),
  ]);

  const waiting = layout(t).phases.filter((p) => p.kind === "waiting");

  assert.equal(waiting.length, 1);
  near(waiting[0].x0, 0.2);
  near(waiting[0].x1, 0.6);
});

test("overlapping prominent markers stack into lanes", () => {
  const same = [
    ev("review_requested", 2 * DAY, { subject: "sam" }),
    ev("review_requested", 2 * DAY, { subject: "lee" }),
    ev("ready_for_review", 2 * DAY),
  ];
  const l = layout(track(10 * DAY, same), { laneGap: 0.05 });

  const lanes = l.markers.filter((m) => m.kind !== "opened").map((m) => m.lane);
  assert.deepEqual(lanes, [0, 1, 2]);
  assert.equal(l.lanes, 3);
  // Far apart: one lane.
  const apart = layout(track(10 * DAY, [same[0], ev("merged", HOUR)]), { laneGap: 0.05 });
  assert.equal(apart.lanes, 1);
});

test("events from before the PR was opened sit at its start", () => {
  const l = layout(track(2 * DAY, [ev("commit", 5 * DAY, { ci: "failed" })]));

  const early = l.markers.find((m) => m.kind === "commit_failed");
  assert.equal(early.x, 0);
  assert.match(early.title, /5d ago/);
});

test("marker tooltips carry the kind, detail, actor and age", () => {
  const l = layout(
    track(4 * DAY, [
      ev("review", 3 * HOUR, { detail: "approved", actor: "sam", subject: "sam" }),
    ]),
  );

  const review = l.markers.find((m) => m.kind === "review_approved");
  assert.equal(review.title, "approved (sam) 3h ago");
});

test("the plain list is chronological and honours the commit toggle", () => {
  const t = track(5 * DAY, [
    ev("review", DAY, { detail: "approved", actor: "sam", subject: "sam" }),
    ev("commit", 4 * DAY, { ci: "failed", detail: "abc fix" }),
  ]);

  const all = tl.listEntries(t, { now: NOW, commits: "all" });
  assert.deepEqual(
    all.map((e) => e.label),
    ["opened", "commit (CI failed)", "approved"],
  );
  assert.equal(all[1].age, "4d ago");
  const none = tl.listEntries(t, { now: NOW, commits: "none" });
  assert.deepEqual(
    none.map((e) => e.kind),
    ["opened", "review_approved"],
  );
});
