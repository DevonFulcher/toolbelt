// Unit tests for the pure half of src/toolbelt/ui/static/stack-table.js.
// Run with `node --test tests/js` (tests/test_ui_script.py does so from pytest).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const table = require("../../src/toolbelt/ui/static/stack-table.js");

const state = (overrides = {}) => ({ ...table.DEFAULT_STATE, ...overrides });

// A row as the DOM layer reads it: only the values the tests care about.
function row(id, parent, order, { search = id, repo = "1", ...cells } = {}) {
  const out = {};
  for (const [col, value] of Object.entries(cells)) {
    out[col] = typeof value === "string" ? { value, sort: value } : value;
  }
  return { id, repo, parent, order, search, cells: out };
}

const ids = (entries) => entries.map((e) => e.id);

test("state round-trips through the query string and omits defaults", () => {
  assert.equal(table.serializeState(table.DEFAULT_STATE), "");
  const full = state({
    view: "table", mode: "flat",
    sort: "ci",
    dir: "desc",
    q: "fix login",
    repo: "3",
    pr: "open",
    ci: "failed",
    review: "approved",
    comments: "has comments",
    merge: "conflicts",
    done: "1",
    compact: "1",
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
  const view = (o) => ids(table.layout(rows, state({ view: "table", mode: "flat", ...o })));
  assert.deepEqual(view({ sort: "comments", dir: "desc" }), ["c", "a", "b", "d"]);
  assert.deepEqual(view({ comments: "has comments" }), ["a", "c"]);
  assert.deepEqual(view({ comments: "none" }), ["b", "d"]);
});

test("parseState ignores unknown sort columns and view names", () => {
  const parsed = table.parseState("?sort=evil&view=3d&dir=sideways&done=yes&group=x");
  assert.deepEqual(parsed, table.DEFAULT_STATE);
});

test("the retired timeline view and its filters are ignored, not an error", () => {
  const parsed = table.parseState("?view=timeline&kind=review&order=oldest&next=you");
  assert.deepEqual(parsed, { ...table.DEFAULT_STATE, next: "you" });
  assert.equal(table.serializeState(parsed), "?next=you");
  assert.deepEqual(table.VIEWS, ["table", "stacked", "grouped"]);
});

test("compact comes from the URL, else from the remembered choice", () => {
  assert.equal(table.initialState("", null).compact, "");
  assert.equal(table.initialState("", "1").compact, "1");
  assert.equal(table.initialState("?compact=1", null).compact, "1");
  // The URL wins over the remembered choice (a bare URL is "not compact"
  // only when it names compact explicitly as absent: here there is no key).
  assert.equal(table.initialState("?view=flat", "1").compact, "1");
  assert.equal(table.initialState("?view=flat", "0").compact, "");
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
];

test("flat view lists the rows in server order by default", () => {
  const out = table.layout(flat, state({ view: "table", mode: "flat" }));
  assert.deepEqual(ids(out), ["a", "b", "c", "d"]);
  assert.ok(out.every((e) => e.depth === 0));
});

test("sorting is numeric and rows without a value stay last in both directions", () => {
  const asc = table.layout(flat, state({ view: "table", mode: "flat", sort: "pr" }));
  assert.deepEqual(ids(asc), ["b", "a", "d", "c"]);
  const desc = table.layout(flat, state({ view: "table", mode: "flat", sort: "pr", dir: "desc" }));
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
  const view = (o) => ids(table.layout(rows, state({ view: "table", mode: "flat", ...o })));
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
  const view = (q) => ids(table.layout(rows, state({ view: "table", mode: "flat", q })));
  assert.deepEqual(view("LOGIN"), ["devon/login"]);
  assert.deepEqual(view("fix"), ["devon/login", "devon/logout"]);
  assert.deepEqual(view("fix logout"), ["devon/logout"]);
  assert.deepEqual(view("#43"), ["devon/logout"]);
  assert.deepEqual(view("abc-1"), ["devon/login"]);
  assert.deepEqual(view("nothing"), []);
});

// (trunk)
// ├─ a ── a1, a2
// └─ b
// The trunk is not a row: a stack's first branch has no parent row.
const tree = [
  row("a", "main", 1, { pr: { value: "open", sort: "30" } }),
  row("a1", "a", 2, { pr: { value: "open", sort: "5" } }),
  row("a2", "a", 3, { pr: { value: "closed", sort: "50" } }),
  row("b", "main", 4, { pr: { value: "open", sort: "10" } }),
];

test("tree view is depth-first from each stack's first branch", () => {
  const out = table.layout(tree, state());
  assert.deepEqual(
    out.map((e) => [e.id, e.depth]),
    [["a", 0], ["a1", 1], ["a2", 1], ["b", 0]],
  );
});

test("tree sort applies among siblings, keeping children under parents", () => {
  const asc = table.layout(tree, state({ sort: "pr" }));
  assert.deepEqual(ids(asc), ["b", "a", "a1", "a2"]);
  const desc = table.layout(tree, state({ sort: "pr", dir: "desc" }));
  assert.deepEqual(ids(desc), ["a", "a2", "a1", "b"]);
});

test("tree filter keeps ancestors of matches for context, flagged", () => {
  const out = table.layout(tree, state({ pr: "closed" }));
  assert.deepEqual(
    out.map((e) => [e.id, e.depth, e.context]),
    [["a", 0, true], ["a2", 1, false]],
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
// repeat (here "x").
const multi = [
  row("1:x", "1:main", 1, { repo: "1", pr: { value: "open", sort: "9" } }),
  row("1:y", "1:main", 2, { repo: "1", pr: { value: "open", sort: "3" } }),
  row("2:x", "2:main", 4, { repo: "2", pr: { value: "merged", sort: "1" } }),
];
for (const r of multi) r.cells.repo = { value: r.repo, sort: r.repo === "1" ? "alpha" : "beta" };

test("tree view keeps each repo's rows together in server order", () => {
  const out = table.layout(multi, state());
  assert.deepEqual(ids(out), ["1:x", "1:y", "2:x"]);
  assert.deepEqual(out.map((e) => e.depth), [0, 0, 0]);
});

test("sorting a column never interleaves repos in tree view", () => {
  const out = table.layout(multi, state({ sort: "pr" }));
  assert.deepEqual(ids(out), ["1:y", "1:x", "2:x"]);
});

test("sorting by repo orders the repo groups; flat view sorts across repos", () => {
  const desc = table.layout(multi, state({ sort: "repo", dir: "desc" }));
  assert.deepEqual(ids(desc), ["2:x", "1:x", "1:y"]);
  const flatByPr = table.layout(multi, state({ view: "table", mode: "flat", sort: "pr" }));
  assert.deepEqual(ids(flatByPr), ["2:x", "1:y", "1:x"]);
});

test("the repo filter scopes the table to one repo", () => {
  assert.deepEqual(ids(table.layout(multi, state({ repo: "2" }))), ["2:x"]);
  assert.deepEqual(ids(table.layout(multi, state({ view: "table", mode: "flat", repo: "1" }))), ["1:x", "1:y"]);
});

test("repo filter combines with value filters", () => {
  const out = table.layout(multi, state({ repo: "1", pr: "open" }));
  assert.deepEqual(ids(out), ["1:x", "1:y"]);
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
  const view = (o) => ids(table.layout(rows, state({ view: "table", mode: "flat", ...o })));
  assert.deepEqual(view({ sort: "next" }), ["b", "d", "c", "a", "e"]);
  assert.deepEqual(view({ next: "you" }), ["b", "d"]);
  assert.equal(table.parseState("?next=you").next, "you");
});

test("the card views live in the URL; retired view names map onto them", () => {
  for (const view of ["stacked", "grouped"]) {
    const query = table.serializeState(state({ view }));
    assert.equal(query, `?view=${view}`);
    assert.equal(table.parseState(query).view, view);
  }
  assert.equal(table.serializeState(state({ view: "table", mode: "flat" })), "?mode=flat");
  assert.equal(table.parseState("?mode=flat").mode, "flat");
  // Old links: view=cards is grouped, view=tree|flat the table, group is ignored.
  assert.equal(table.parseState("?view=cards").view, "grouped");
  assert.deepEqual(table.parseState("?view=tree"), table.DEFAULT_STATE);
  const flat = table.parseState("?view=flat");
  assert.equal(flat.view, "table");
  assert.equal(flat.mode, "flat");
  assert.deepEqual(table.parseState("?view=cards&group=repo"), state({ view: "grouped" }));
  const full = state({ view: "grouped", q: "auth", next: "you", done: "1", sort: "ci", dir: "desc" });
  assert.deepEqual(table.parseState(table.serializeState(full)), full);
});

const nextRow = (id, parent, order, who, extra = {}) =>
  row(id, parent, order, { next: { value: who, sort: "" }, ...extra });

test("grouped cards are sections by next, in order, empty ones omitted", () => {
  const rows = [
    nextRow("a", "main", 1, "others"),
    nextRow("b", "a", 2, "you"),
    nextRow("c", "main", 3, "ci"),
    nextRow("d", "c", 4, "you"),
    nextRow("e", "main", 5, "none"),
  ];
  const sections = (o) => table.groupedLayout(rows, state({ view: "grouped", ...o }));

  const out = sections({});
  assert.deepEqual(out.map((s) => s.key), ["you", "ci", "others"]);
  assert.deepEqual(out.map((s) => s.label), ["Needs me", "Waiting on CI", "Waiting on others / team"]);
  assert.ok(out.every((s) => s.meaning.length > 0));
  assert.deepEqual(ids(out[0].entries), ["b", "d"]);
  // Done is hidden unless included; filters apply as in the table.
  assert.deepEqual(sections({ done: "1" }).map((s) => s.key), ["you", "ci", "others", "none"]);
  assert.deepEqual(sections({ next: "you" }).map((s) => s.key), ["you"]);
  assert.ok(out.every((s) => s.entries.every((e) => e.depth === 0 && !e.context)));
});

test("within a grouped section branches follow stack order; sort applies among siblings", () => {
  const rows = [
    nextRow("1:a", "", 1, "you", { repo: "1", comments: { value: "x", sort: "5" } }),
    nextRow("1:b", "1:a", 2, "you", { repo: "1", comments: { value: "x", sort: "1" } }),
    nextRow("1:c", "", 3, "you", { repo: "1", comments: { value: "x", sort: "2" } }),
    nextRow("2:d", "", 4, "you", { repo: "2", comments: { value: "x", sort: "0" } }),
  ];

  const def = table.groupedLayout(rows, state({ view: "grouped" }));
  assert.deepEqual(ids(def[0].entries), ["1:a", "1:b", "1:c", "2:d"]);

  const sorted = table.groupedLayout(rows, state({ view: "grouped", sort: "comments" }));
  // Roots by comments (c before a); a's child stays right under it.
  assert.deepEqual(ids(sorted[0].entries), ["1:c", "1:a", "1:b", "2:d"]);
});

test("a branch whose next is not known yet is in a trailing Loading section", () => {
  const rows = [nextRow("a", "main", 1, ""), nextRow("b", "main", 2, "you")];
  const out = table.groupedLayout(rows, state({ view: "grouped" }));
  assert.deepEqual(out.map((s) => s.key), ["you", "loading"]);
});

test("stacked cards keep each repo's stacks together, indented by depth, finished parents as context", () => {
  const rows = [
    nextRow("1:a", "", 1, "none", { repo: "1" }),
    nextRow("1:b", "1:a", 2, "you", { repo: "1" }),
    nextRow("2:c", "", 3, "you", { repo: "2" }),
    nextRow("1:d", "", 4, "ci", { repo: "1" }),
  ];

  const out = table.stackedLayout(rows, state({ view: "stacked" }));

  assert.deepEqual(out.map((s) => s.key), ["repo:1", "repo:2"]);
  assert.deepEqual(
    out[0].entries.map((e) => [e.id, e.depth, e.context]),
    [["1:a", 0, true], ["1:b", 1, false], ["1:d", 0, false]],
  );
  // With done included the parent is a card of its own.
  const done = table.stackedLayout(rows, state({ view: "stacked", done: "1" }));
  assert.equal(done[0].entries[0].context, false);
});

test("rails: tees for siblings that continue, corners for the last, lines through subtrees", () => {
  // a
  //   b
  //     c
  //   d
  //     e
  const entries = [0, 1, 2, 1, 2].map((depth) => ({ depth }));

  const guides = table.railGuides(entries);

  assert.deepEqual(guides[0], []);
  assert.deepEqual(guides[1], [{ col: 0, kind: "tee" }]);
  // c: b has a later sibling (d), so column 0 passes through; c is last under b.
  assert.deepEqual(guides[2], [
    { col: 0, kind: "line" },
    { col: 1, kind: "corner" },
  ]);
  assert.deepEqual(guides[3], [{ col: 0, kind: "corner" }]);
  // e: d is the last of a's children: no line in column 0.
  assert.deepEqual(guides[4], [{ col: 1, kind: "corner" }]);
});

const withNext = (value) => ({ next: { value, sort: "" } });

test("done branches are hidden unless included or asked for by the next filter", () => {
  const rows = [
    row("a", "main", 1, withNext("you")),
    row("b", "main", 2, withNext("none")),
    row("c", "main", 3, withNext("")),
  ];
  const view = (o) => ids(table.layout(rows, state({ view: "table", mode: "flat", ...o })));
  assert.deepEqual(view({}), ["a", "c"]);
  assert.deepEqual(view({ done: "1" }), ["a", "b", "c"]);
  assert.deepEqual(view({ next: "none" }), ["b"]);
  assert.deepEqual(view({ next: "you" }), ["a"]);
});

test("a done parent stays as context for an open child", () => {
  const rows = [
    row("a", "main", 1, withNext("none")),
    row("b", "a", 2, withNext("you")),
  ];
  const out = table.layout(rows, state());
  assert.deepEqual(
    out.map((e) => [e.id, e.depth, e.context]),
    [["a", 0, true], ["b", 1, false]],
  );
  assert.equal(table.matches(rows[0], state()), false);
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
