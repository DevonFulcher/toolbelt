// Unit tests for the pure half of src/toolbelt/ui/static/pr-timeline.js:
// where a PR's events, phases and clusters sit along its per-card track.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { catalog } from "./state-catalog.mjs";

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

const layout = (t, options = {}) => tl.trackLayout(t, { now: NOW, catalog, ...options });
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
      ["approved", 0.5, 1],
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

  const all = tl.listEntries(t, { now: NOW, catalog, commits: "all" });
  assert.deepEqual(
    all.map((e) => e.label),
    ["opened", "commit (CI failed)", "approved"],
  );
  assert.equal(all[1].age, "4d ago");
  const none = tl.listEntries(t, { now: NOW, catalog, commits: "none" });
  assert.deepEqual(
    none.map((e) => e.kind),
    ["opened", "review_approved"],
  );
});

// ---- relative / absolute time ------------------------------------------------

test("spans read as the two largest units; absolute times as a local date", () => {
  assert.equal(tl.formatSpan(30), "<1m");
  assert.equal(tl.formatSpan(5 * 60), "5m");
  assert.equal(tl.formatSpan(3 * 3600 + 12 * 60), "3h 12m");
  assert.equal(tl.formatSpan(3600), "1h");
  assert.equal(tl.formatSpan(2 * 86400 + 4 * 3600 + 59), "2d 4h");
  assert.equal(tl.formatSpan(23 * 86400), "3w 2d");
  assert.equal(
    tl.formatAbsolute(Date.parse("2026-10-08T15:42:00Z"), { timeZone: "UTC" }),
    "Oct 8, 2026, 3:42 PM",
  );
  assert.equal(
    tl.formatAbsolute(Date.parse("2026-10-08T15:42:00Z"), { timeZone: "America/Chicago" }),
    "Oct 8, 2026, 10:42 AM",
  );
});

// ---- approved phase ---------------------------------------------------------

const approvedSpans = (t) => layout(t).phases.filter((p) => p.kind === "approved");
const approve = (agoMs) => ev("review", agoMs, { detail: "approved", actor: "sam", subject: "sam" });

test("approved runs from the approving review to now", () => {
  const spans = approvedSpans(track(10 * DAY, [approve(4 * DAY)], { review: "approved" }));

  assert.equal(spans.length, 1);
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
  assert.equal(spans.length, 1);
  near(spans[0].x0, 0.4);
  near(spans[0].x1, 0.7);
});

test("an approval that is stale now ends at the first push after it", () => {
  const events = [approve(6 * DAY), ev("commit", 4 * DAY, { ci: "success" })];

  const stale = approvedSpans(track(10 * DAY, events, { review: "none" }));
  assert.equal(stale.length, 1);
  near(stale[0].x1, 0.6);

  // Still approved now: a later push does not end it.
  const still = approvedSpans(track(10 * DAY, events, { review: "approved" }));
  near(still[0].x1, 1);
});

// ---- chips ------------------------------------------------------------------

test("now chips come from catalog references and are labelled by group", () => {
  const t = track(DAY, [], {
    current: [
      { dimension: "pr", value: "open", args: {} },
      { dimension: "ci", value: "running", args: {} },
      { dimension: "comments", value: "open", args: { n: 3 } },
      { dimension: "merge", value: "conflicts", args: {} },
      { dimension: "next", value: "blocked", args: { text: "Blocked on devon/a", who: "you" } },
    ],
  });

  const chips = tl.nowChips(t, catalog);

  assert.deepEqual(
    chips.map((c) => [c.group, c.text, c.tone]),
    [
      ["PR", "open", "green"],
      ["CI", "running", "amber"],
      ["Comments", "3 unresolved", "amber"],
      ["Merge", "conflicts", "red"],
      ["Next", "Blocked on devon/a", "accent"],
    ],
  );
});

test("an unknown state or event kind is an error, not a blank", () => {
  const t = track(DAY, [], { current: [{ dimension: "ci", value: "exploded", args: {} }] });
  assert.throws(() => tl.nowChips(t, catalog), /no catalog entry for ci=exploded/);
  const odd = track(DAY, [ev("teleported", HOUR)]);
  assert.throws(() => layout(odd), /no catalog entry for event kind "teleported"/);
});

// ---- tooltips -----------------------------------------------------------------

test("an event tooltip has kind, detail, actor, absolute and relative time", () => {
  const l = layout(
    track(4 * DAY, [
      ev("commit", 2 * DAY + 4 * HOUR, { detail: "abc1234 Fix it", ci: "failed", actor: "devon" }),
    ]),
  );
  const marker = l.markers.find((m) => m.kind === "commit_failed");

  const content = tl.tooltipContent(marker.tip, NOW, { timeZone: "UTC" });

  assert.equal(content.title, "commit (CI failed)");
  assert.equal(content.subtitle, "abc1234 Fix it");
  assert.deepEqual(content.rows, [
    ["Actor", "devon"],
    ["CI", "failed"],
    ["When", "Oct 18, 2026, 8:00 AM"],
    ["Ago", "2d 4h ago"],
  ]);
});

test("a cluster tooltip lists its members and the range", () => {
  const burst = [];
  for (let i = 0; i < 12; i += 1) burst.push(ev("commit", 10 * DAY - i * MIN, { detail: `c${i}` }));
  const l = layout(track(30 * DAY, burst), { commits: "all" });
  const cluster = l.markers.find((m) => m.kind === "commits");

  const content = tl.tooltipContent(cluster.tip, NOW, { timeZone: "UTC" });

  assert.equal(content.title, "12 commits");
  assert.deepEqual(
    content.rows.map(([k]) => k),
    ["From", "To"],
  );
  assert.equal(content.items.length, 9);
  assert.match(content.items[0], /devon · c0$/);
  assert.equal(content.items[8], "+ 4 more");
});

test("a phase tooltip has its start, end and duration", () => {
  const t = track(10 * DAY, [ev("commit", 8 * DAY, { ci: "failed" }), ev("commit", 6 * DAY)]);
  const failing = layout(t).phases.find((p) => p.kind === "ci_failing");

  const content = tl.tooltipContent(failing.tip, NOW, { timeZone: "UTC" });

  assert.equal(content.title, "CI failing");
  assert.deepEqual(
    content.rows.map(([k]) => k),
    ["From", "To", "Duration"],
  );
  assert.equal(content.rows[2][1], "2d");
  assert.match(content.rows[1][1], /\(6d ago\)$/);
  // A span running to now says so.
  const ongoing = layout(track(3 * DAY, [], { draft: true })).phases[0];
  assert.equal(tl.tooltipContent(ongoing.tip, NOW).rows[1][1], "now");
});

test("tooltips are placed below, flipped above at the bottom, clamped sideways", () => {
  const size = { width: 200, height: 100 };
  const viewport = { width: 800, height: 600 };
  const rect = (left, top) => ({ left, right: left + 10, top, bottom: top + 10 });

  const below = tl.positionTip(rect(300, 100), size, viewport);
  assert.equal(below.placement, "below");
  assert.equal(below.top, 118);
  assert.equal(below.left, 205);

  const above = tl.positionTip(rect(300, 560), size, viewport);
  assert.equal(above.placement, "above");
  assert.equal(above.top, 560 - 8 - 100);

  assert.equal(tl.positionTip(rect(0, 100), size, viewport).left, 4);
  assert.equal(tl.positionTip(rect(795, 100), size, viewport).left, 800 - 200 - 4);
});

// ---- catalog exhaustiveness: the renderer draws whatever the catalog says --------

// A minimal document: enough of the DOM for render() to build a tree.
function fakeDocument() {
  const make = (tag) => ({
    tag,
    className: "",
    textContent: "",
    children: [],
    style: { setProperty() {} },
    attributes: {},
    dataset: {},
    append(...kids) {
      for (const kid of kids) {
        this.children.push(
          typeof kid === "string" ? { tag: "#text", textContent: kid, children: [] } : kid,
        );
      }
    },
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
  });
  return { createElement: make };
}

function walk(node, visit) {
  visit(node);
  for (const child of node.children || []) walk(child, visit);
}

// The server event that produces each marker kind (opened is implicit).
const SERVER_EVENTS = {
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

test("every catalog event kind is one the renderer produces, and is drawn with its glyph", () => {
  // No catalog event the renderer cannot draw (the cluster is made of commits).
  const known = new Set(["opened", "commits", ...Object.keys(SERVER_EVENTS)]);
  assert.deepEqual(Object.keys(catalog.events).filter((k) => !known.has(k)), []);
  assert.deepEqual([...known].filter((k) => !(k in catalog.events)), []);

  for (const [kind, spec] of Object.entries(SERVER_EVENTS)) {
    assert.equal(tl.markerKind({ ...spec, detail: spec.detail || "" }), kind);
  }
  const events = Object.values(SERVER_EVENTS).map((spec, i) => ev(spec.kind, 30 * DAY - i * DAY, spec));
  const tree = tl.render(fakeDocument(), track(40 * DAY, events, { current: [] }), {
    now: NOW,
    catalog,
    expanded: true,
    legend: true,
  });

  const glyphs = [];
  walk(tree, (n) => {
    if ((n.className || "").includes("tl-mark")) glyphs.push(n.textContent);
  });
  for (const [kind, info] of Object.entries(catalog.events)) {
    if (kind === "commits" || kind === "commit") continue;
    assert.ok(glyphs.includes(info.glyph), kind);
  }
});

test("every catalog state is representable: its kinds exist and it can be a chip", () => {
  for (const state of catalog.states) {
    assert.ok(state.reps.length > 0, `${state.dimension}=${state.value} has no representation`);
    for (const rep of state.reps) {
      const vocabulary = { event: catalog.events, phase: catalog.phases, now_state: catalog.chips }[rep.how];
      assert.ok(vocabulary && rep.kind in vocabulary, `${state.dimension}=${state.value}: ${rep.how} ${rep.kind}`);
    }
    if (state.dimension in catalog.chips) {
      const args = { n: 2, text: "label", who: "you" };
      const current = [{ dimension: state.dimension, value: state.value, args }];
      const [chip] = tl.nowChips(track(DAY, [], { current }), catalog);
      assert.ok(chip.text && chip.group, `${state.dimension}=${state.value}`);
    }
  }
});

test("the legend lists every catalog event, phase and chip value, and what is not shown", () => {
  const legend = tl.legendModel(catalog);

  assert.deepEqual(legend.events.map((e) => e.kind), Object.keys(catalog.events));
  assert.deepEqual(legend.phases.map((p) => p.kind), Object.keys(catalog.phases));
  assert.deepEqual(legend.chips.map((c) => c.dimension), Object.keys(catalog.chips));
  for (const group of legend.chips) {
    const expected = catalog.states.filter((s) => s.dimension === group.dimension);
    assert.equal(group.values.length, expected.length, group.dimension);
  }
  assert.ok(legend.notRepresented.some((n) => /conflict/i.test(n.label)));
  assert.ok(legend.notRepresented.some((n) => /thread/i.test(n.label)));
});

test("a card with no PR renders its chips and says there is no history", () => {
  const none = {
    created: null,
    ended: null,
    state: "none",
    draft: false,
    review: "none",
    events: [],
    current: [
      { dimension: "pr", value: "none", args: {} },
      { dimension: "next", value: "open_pr", args: { text: "Open PR", who: "you" } },
    ],
  };

  const tree = tl.render(fakeDocument(), none, { now: NOW, catalog, expanded: false });

  const texts = [];
  walk(tree, (n) => n.textContent && texts.push(n.textContent));
  assert.ok(texts.some((t) => t.includes("no PR yet")));
  assert.ok(texts.includes("PR") && texts.includes("Next"));
});
