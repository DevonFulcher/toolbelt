// Unit tests for the pure half of src/toolbelt/ui/static/stack-table.js.
// Run with `node --test tests/js` (tests/test_ui_script.py does so from pytest).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const table = require("../../src/toolbelt/ui/static/stack-table.js");

const state = (overrides = {}) => ({ ...table.DEFAULT_STATE, ...overrides });

// A row as the DOM layer reads it: only the values the tests care about.
function row(
  id,
  parent,
  order,
  { tracked = true, search = id, repo = "1", ...cells } = {},
) {
  const out = {};
  for (const [col, value] of Object.entries(cells)) {
    out[col] = typeof value === "string" ? { value, sort: value } : value;
  }
  return { id, repo, parent, order, tracked, search, cells: out };
}

const ids = (entries) => entries.map((e) => e.id);

test("state round-trips through the query string and omits defaults", () => {
  assert.equal(table.serializeState(table.DEFAULT_STATE), "");
  const full = state({
    view: "flat",
    sort: "ci",
    dir: "desc",
    q: "fix login",
    repo: "3",
    pr: "open",
    ci: "failed",
    review: "approved",
    comments: "has comments",
    merge: "conflicts",
  });
  const query = table.serializeState(full);
  assert.deepEqual(table.parseState(query), full);
  // Direction is only meaningful with a sort column.
  assert.equal(table.serializeState(state({ dir: "desc" })), "");
});

test("the comments column is sortable and filterable", () => {
  assert.ok(table.COLUMNS.includes("comments"));
  assert.ok(table.VALUE_FILTERS.includes("comments"));
  assert.ok(table.COLUMNS.includes("repo"));
  const rows = [
    row("a", "main", 1, { comments: { value: "has comments", sort: "3" } }),
    row("b", "main", 2, { comments: { value: "none", sort: "0" } }),
    row("c", "main", 3, { comments: { value: "has comments", sort: "12" } }),
    row("d", "main", 4, { comments: { value: "none", sort: "" } }),
  ];
  const view = (o) => ids(table.layout(rows, state({ view: "flat", ...o })));
  assert.deepEqual(view({ sort: "comments", dir: "desc" }), ["c", "a", "b", "d"]);
  assert.deepEqual(view({ comments: "has comments" }), ["a", "c"]);
  assert.deepEqual(view({ comments: "none" }), ["b", "d"]);
});

test("parseState ignores unknown sort columns and view names", () => {
  const parsed = table.parseState("?sort=evil&view=3d&dir=sideways");
  assert.deepEqual(parsed, table.DEFAULT_STATE);
});

test("toggleSort starts ascending and flips on the same column", () => {
  let s = table.toggleSort(state(), "pr");
  assert.deepEqual([s.sort, s.dir], ["pr", "asc"]);
  s = table.toggleSort(s, "pr");
  assert.deepEqual([s.sort, s.dir], ["pr", "desc"]);
  s = table.toggleSort(s, "ci");
  assert.deepEqual([s.sort, s.dir], ["ci", "asc"]);
});

test("compareKeys is numeric for numbers and natural for text", () => {
  assert.ok(table.compareKeys("9", "10") < 0);
  assert.ok(table.compareKeys("ABC-9", "ABC-10") < 0);
  assert.ok(table.compareKeys("abc", "ABD") < 0);
});

const flat = [
  row("a", "main", 1, { pr: { value: "open", sort: "12" } }),
  row("b", "main", 2, { pr: { value: "merged", sort: "9" } }),
  row("c", "main", 3, { pr: { value: "none", sort: "" } }),
  row("d", "main", 4, { pr: { value: "open", sort: "101" } }),
  row("main", null, 0, { tracked: false }),
];

test("flat view lists only tracked rows, in server order by default", () => {
  const out = table.layout(flat, state({ view: "flat" }));
  assert.deepEqual(ids(out), ["a", "b", "c", "d"]);
  assert.ok(out.every((e) => e.depth === 0));
});

test("sorting is numeric and rows without a value stay last in both directions", () => {
  const asc = table.layout(flat, state({ view: "flat", sort: "pr" }));
  assert.deepEqual(ids(asc), ["b", "a", "d", "c"]);
  const desc = table.layout(flat, state({ view: "flat", sort: "pr", dir: "desc" }));
  assert.deepEqual(ids(desc), ["d", "a", "b", "c"]);
});

test("value filters match exactly and combine", () => {
  const rows = [
    row("a", "main", 1, { pr: "open", ci: "failed" }),
    row("b", "main", 2, { pr: "open", ci: "success" }),
    row("c", "main", 3, { pr: "merged", ci: "failed" }),
    // Still loading: empty values never match a chosen filter.
    row("d", "main", 4, { pr: "", ci: "" }),
  ];
  const view = (o) => ids(table.layout(rows, state({ view: "flat", ...o })));
  assert.deepEqual(view({ pr: "open" }), ["a", "b"]);
  assert.deepEqual(view({ ci: "failed" }), ["a", "c"]);
  assert.deepEqual(view({ pr: "open", ci: "failed" }), ["a"]);
  assert.deepEqual(view({}), ["a", "b", "c", "d"]);
});

test("text filter is case-insensitive, ANDs terms, and accepts #PR numbers", () => {
  const rows = [
    row("devon/login", "main", 1, { search: "devon/login 42 ABC-1 Fix login bug" }),
    row("devon/logout", "main", 2, { search: "devon/logout 43 ABC-2 Fix logout" }),
    row("devon/docs", "main", 3, { search: "devon/docs" }),
  ];
  const view = (q) => ids(table.layout(rows, state({ view: "flat", q })));
  assert.deepEqual(view("LOGIN"), ["devon/login"]);
  assert.deepEqual(view("fix"), ["devon/login", "devon/logout"]);
  assert.deepEqual(view("fix logout"), ["devon/logout"]);
  assert.deepEqual(view("#43"), ["devon/logout"]);
  assert.deepEqual(view("abc-1"), ["devon/login"]);
  assert.deepEqual(view("nothing"), []);
});

// main
// ├─ a ── a1, a2
// └─ b
const tree = [
  row("main", null, 0, { tracked: false, pr: "" }),
  row("a", "main", 1, { pr: { value: "open", sort: "30" } }),
  row("a1", "a", 2, { pr: { value: "open", sort: "5" } }),
  row("a2", "a", 3, { pr: { value: "closed", sort: "50" } }),
  row("b", "main", 4, { pr: { value: "open", sort: "10" } }),
];

test("tree view is depth-first with depths, bases included", () => {
  const out = table.layout(tree, state());
  assert.deepEqual(
    out.map((e) => [e.id, e.depth]),
    [["main", 0], ["a", 1], ["a1", 2], ["a2", 2], ["b", 1]],
  );
});

test("tree sort applies among siblings, keeping children under parents", () => {
  const asc = table.layout(tree, state({ sort: "pr" }));
  assert.deepEqual(ids(asc), ["main", "b", "a", "a1", "a2"]);
  const desc = table.layout(tree, state({ sort: "pr", dir: "desc" }));
  assert.deepEqual(ids(desc), ["main", "a", "a2", "a1", "b"]);
});

test("tree filter keeps ancestors of matches for context", () => {
  const out = table.layout(tree, state({ pr: "closed" }));
  assert.deepEqual(
    out.map((e) => [e.id, e.depth]),
    [["main", 0], ["a", 1], ["a2", 2]],
  );
});

test("tree filter with no matches shows nothing", () => {
  assert.deepEqual(table.layout(tree, state({ q: "zzz" })), []);
});

test("rows whose parent is missing become roots; cycles do not hang", () => {
  const rows = [
    row("x", "gone", 0, {}),
    row("y", "z", 1, {}),
    row("z", "y", 2, {}),
  ];
  const out = table.layout(rows, state());
  assert.deepEqual(ids(out), ["x"]);
});

// Two repos in one table: ids are unique across repos, so a branch name can
// repeat (here "main" and "x").
const multi = [
  row("1:main", null, 0, { tracked: false, repo: "1", pr: "" }),
  row("1:x", "1:main", 1, { repo: "1", pr: { value: "open", sort: "9" } }),
  row("1:y", "1:main", 2, { repo: "1", pr: { value: "open", sort: "3" } }),
  row("2:main", null, 3, { tracked: false, repo: "2", pr: "" }),
  row("2:x", "2:main", 4, { repo: "2", pr: { value: "merged", sort: "1" } }),
];
multi[0].cells.repo = { value: "1", sort: "alpha" };
for (const r of multi) r.cells.repo = { value: r.repo, sort: r.repo === "1" ? "alpha" : "beta" };

test("tree view keeps each repo's rows together in server order", () => {
  const out = table.layout(multi, state());
  assert.deepEqual(ids(out), ["1:main", "1:x", "1:y", "2:main", "2:x"]);
  assert.deepEqual(out.map((e) => e.depth), [0, 1, 1, 0, 1]);
});

test("sorting a column never interleaves repos in tree view", () => {
  const out = table.layout(multi, state({ sort: "pr" }));
  assert.deepEqual(ids(out), ["1:main", "1:y", "1:x", "2:main", "2:x"]);
});

test("sorting by repo orders the repo groups; flat view sorts across repos", () => {
  const desc = table.layout(multi, state({ sort: "repo", dir: "desc" }));
  assert.deepEqual(ids(desc), ["2:main", "2:x", "1:main", "1:x", "1:y"]);
  const flatByRepo = table.layout(multi, state({ view: "flat", sort: "repo", dir: "desc" }));
  assert.deepEqual(ids(flatByRepo), ["2:x", "1:x", "1:y"]);
  const flatByPr = table.layout(multi, state({ view: "flat", sort: "pr" }));
  assert.deepEqual(ids(flatByPr), ["2:x", "1:y", "1:x"]);
});

test("the repo filter scopes the table to one repo, bases included in tree view", () => {
  const tree = table.layout(multi, state({ repo: "2" }));
  assert.deepEqual(ids(tree), ["2:main", "2:x"]);
  const flat = table.layout(multi, state({ view: "flat", repo: "1" }));
  assert.deepEqual(ids(flat), ["1:x", "1:y"]);
});

test("repo filter combines with value filters and keeps ancestors", () => {
  const out = table.layout(multi, state({ repo: "1", pr: "open" }));
  assert.deepEqual(ids(out), ["1:main", "1:x", "1:y"]);
  assert.deepEqual(table.layout(multi, state({ repo: "2", pr: "open" })), []);
});

test("the next column floats rows that need the user first and filters by who", () => {
  assert.ok(table.COLUMNS.includes("next"));
  assert.ok(table.VALUE_FILTERS.includes("next"));
  const rows = [
    row("a", "main", 1, { next: { value: "others", sort: "3" } }),
    row("b", "main", 2, { next: { value: "you", sort: "1" } }),
    row("c", "main", 3, { next: { value: "ci", sort: "2" } }),
    row("d", "main", 4, { next: { value: "you", sort: "1" } }),
    row("e", "main", 5, { next: { value: "", sort: "" } }),
  ];
  const view = (o) => ids(table.layout(rows, state({ view: "flat", ...o })));
  assert.deepEqual(view({ sort: "next" }), ["b", "d", "c", "a", "e"]);
  assert.deepEqual(view({ next: "you" }), ["b", "d"]);
  assert.equal(table.parseState("?next=you").next, "you");
});

test("the cards and timeline views live in the URL next to tree and flat", () => {
  for (const view of ["flat", "cards", "timeline"]) {
    const query = table.serializeState(state({ view }));
    assert.equal(query, `?view=${view}`);
    assert.equal(table.parseState(query).view, view);
  }
  assert.equal(table.parseState("?view=tree").view, "tree");
  const full = state({ view: "timeline", kind: "review", order: "oldest", q: "auth", next: "you" });
  assert.deepEqual(table.parseState(table.serializeState(full)), full);
  // The defaults stay out of the URL.
  assert.equal(table.serializeState(state({ view: "timeline" })), "?view=timeline");
});

test("cards are flat, filtered, and by default list what needs you first", () => {
  const rows = [
    row("a", "main", 1, { next: { value: "others", sort: "3" } }),
    row("b", "a", 2, { next: { value: "you", sort: "1" } }),
    row("c", "main", 3, { next: { value: "ci", sort: "2" } }),
    row("d", "c", 4, { next: { value: "you", sort: "1" } }),
  ];
  const cards = (o) => table.layout(rows, state({ view: "cards", ...o }));
  assert.deepEqual(ids(cards({})), ["b", "d", "c", "a"]);
  // Never indented: a card grid has no depth.
  assert.ok(cards({}).every((entry) => entry.depth === 0));
  // The sort select overrides the default; filters apply as in the table.
  assert.deepEqual(ids(cards({ sort: "next", dir: "desc" })), ["a", "c", "b", "d"]);
  assert.deepEqual(ids(cards({ next: "you" })), ["b", "d"]);
});

function withEvents(r, events, extra = {}) {
  return { ...r, branch: r.id, repoName: "api", number: "1", url: "", events, ...extra };
}

test("the timeline merges every branch's events, newest first", () => {
  const rows = [
    withEvents(row("a", "main", 1), [
      { t: "2026-10-01T10:00:00Z", kind: "opened", actor: "devon", detail: "A" },
      { t: "2026-10-03T10:00:00Z", kind: "commit", actor: "devon", detail: "c1" },
    ]),
    withEvents(row("b", "main", 2), [
      { t: "2026-10-02T10:00:00Z", kind: "review", actor: "sam", detail: "approved" },
    ]),
  ];

  const newest = table.timeline(rows, state({ view: "timeline" }));
  assert.deepEqual(newest.entries.map((e) => `${e.id}:${e.kind}`), [
    "a:commit",
    "b:review",
    "a:opened",
  ]);
  assert.equal(newest.total, 3);

  const oldest = table.timeline(rows, state({ view: "timeline", order: "oldest" }));
  assert.deepEqual(oldest.entries.map((e) => e.kind), ["opened", "review", "commit"]);
});

test("the timeline filters by event kind, branch text, repo and the other filters", () => {
  const events = [
    { t: "2026-10-01T10:00:00Z", kind: "opened", actor: "devon", detail: "" },
    { t: "2026-10-02T10:00:00Z", kind: "review", actor: "sam", detail: "approved" },
  ];
  const rows = [
    withEvents(row("a", "main", 1, { search: "auth-api", repo: "1", ci: "failed" }), events),
    withEvents(row("b", "main", 2, { search: "auth-ui", repo: "2", ci: "success" }), events),
    // An untracked base branch has no events of its own to show.
    withEvents(row("main", null, 0, { tracked: false }), events),
  ];
  const kinds = (o) =>
    table.timeline(rows, state({ view: "timeline", ...o })).entries.map((e) => `${e.id}:${e.kind}`);
  assert.deepEqual(kinds({ kind: "review" }), ["b:review", "a:review"]);
  assert.deepEqual(kinds({ q: "auth-api" }), ["a:review", "a:opened"]);
  assert.deepEqual(kinds({ repo: "2", kind: "opened" }), ["b:opened"]);
  assert.deepEqual(kinds({ ci: "failed", kind: "review" }), ["a:review"]);
});

test("the timeline is cut to the most recent events", () => {
  const events = [];
  for (let i = 0; i < 250; i += 1) {
    const day = String(1 + Math.floor(i / 24)).padStart(2, "0");
    const hour = String(i % 24).padStart(2, "0");
    events.push({ t: `2026-10-${day}T${hour}:00:00Z`, kind: "commit", actor: "d", detail: `${i}` });
  }
  const rows = [withEvents(row("a", "main", 1), events)];

  const result = table.timeline(rows, state({ view: "timeline" }));

  assert.equal(result.total, 250);
  assert.equal(result.entries.length, table.TIMELINE_LIMIT);
  assert.equal(result.entries[0].detail, "249");
  assert.equal(result.entries[result.entries.length - 1].detail, "50");
  // Oldest-first reads the same window from its start.
  const oldest = table.timeline(rows, state({ view: "timeline", order: "oldest" }));
  assert.equal(oldest.entries[0].detail, "50");
});

test("formatAge matches the server's compact ages", () => {
  assert.equal(table.formatAge(30), "<1m");
  assert.equal(table.formatAge(300), "5m");
  assert.equal(table.formatAge(3 * 3600), "3h");
  assert.equal(table.formatAge(23 * 3600), "23h");
  assert.equal(table.formatAge(86400), "1d");
  assert.equal(table.formatAge(13 * 86400), "13d");
  assert.equal(table.formatAge(14 * 86400), "2w");
  assert.equal(table.formatAge(75 * 86400), "10w");
  assert.equal(table.formatAge(-5), "<1m");
});
