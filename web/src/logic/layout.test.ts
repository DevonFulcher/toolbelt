import { describe, expect, test } from "vitest";
import {
  compareKeys,
  groupedLayout,
  layout,
  matches,
  NEXT_SECTIONS,
  railGuides,
  stackedLayout,
} from "./layout";
import type { Cell, Row } from "./rows";
import { COLUMNS, DEFAULT_STATE, type Column, type ViewState } from "./viewState";

const state = (overrides: Partial<ViewState> = {}): ViewState => ({ ...DEFAULT_STATE, ...overrides });

// A row as the layout reads it: only the values the tests care about.
type CellInput = string | Cell;
function row(
  id: string,
  parent: string | null,
  order: number,
  { search = id, repo = "1", ...cells }: { search?: string; repo?: string } & Partial<Record<Column, CellInput>> = {},
): Row {
  const out: Partial<Record<Column, Cell>> = {};
  for (const [column, value] of Object.entries(cells) as [Column, CellInput][]) {
    out[column] = typeof value === "string" ? { value, sort: value } : value;
  }
  return { id, repo, repoName: repo, branch: id, parent, order, search, cells: out };
}

const ids = (entries: { id: string }[]): string[] => entries.map((e) => e.id);
const flatTable = { view: "table", mode: "flat" } as const;

describe("sorting and filtering", () => {
  test("the comments column is sortable and filterable", () => {
    expect(COLUMNS).toContain("comments");
    const rows = [
      row("a", null, 1, { comments: { value: "has comments", sort: "3" } }),
      row("b", null, 2, { comments: { value: "none", sort: "0" } }),
      row("c", null, 3, { comments: { value: "has comments", sort: "12" } }),
      row("d", null, 4, { comments: { value: "none", sort: "" } }),
    ];
    const view = (o: Partial<ViewState>) => ids(layout(rows, state({ ...flatTable, ...o })));
    expect(view({ sort: "comments", dir: "desc" })).toEqual(["c", "a", "b", "d"]);
    expect(view({ comments: "has comments" })).toEqual(["a", "c"]);
    expect(view({ comments: "none" })).toEqual(["b", "d"]);
  });

  test("compareKeys is numeric for numbers and natural for text", () => {
    expect(compareKeys("9", "10")).toBeLessThan(0);
    expect(compareKeys("ABC-9", "ABC-10")).toBeLessThan(0);
    expect(compareKeys("abc", "ABD")).toBeLessThan(0);
  });

  const flat = [
    row("a", null, 1, { pr: { value: "open", sort: "12" } }),
    row("b", null, 2, { pr: { value: "merged", sort: "9" } }),
    row("c", null, 3, { pr: { value: "none", sort: "" } }),
    row("d", null, 4, { pr: { value: "open", sort: "101" } }),
  ];

  test("the flat view lists the rows in server order by default", () => {
    const out = layout(flat, state(flatTable));
    expect(ids(out)).toEqual(["a", "b", "c", "d"]);
    expect(out.every((e) => e.depth === 0)).toBe(true);
  });

  test("sorting is numeric and rows without a value stay last in both directions", () => {
    expect(ids(layout(flat, state({ ...flatTable, sort: "pr" })))).toEqual(["b", "a", "d", "c"]);
    expect(ids(layout(flat, state({ ...flatTable, sort: "pr", dir: "desc" })))).toEqual(["d", "a", "b", "c"]);
  });

  test("value filters match exactly and combine", () => {
    const rows = [
      row("a", null, 1, { pr: "open", ci: "failed" }),
      row("b", null, 2, { pr: "open", ci: "success" }),
      row("c", null, 3, { pr: "merged", ci: "failed" }),
      // Still loading: empty values never match a chosen filter.
      row("d", null, 4, { pr: "", ci: "" }),
    ];
    const view = (o: Partial<ViewState>) => ids(layout(rows, state({ ...flatTable, ...o })));
    expect(view({ pr: "open" })).toEqual(["a", "b"]);
    expect(view({ ci: "failed" })).toEqual(["a", "c"]);
    expect(view({ pr: "open", ci: "failed" })).toEqual(["a"]);
    expect(view({})).toEqual(["a", "b", "c", "d"]);
  });

  test("the text filter is case-insensitive, ANDs terms, and accepts #PR numbers", () => {
    const rows = [
      row("devon/login", null, 1, { search: "devon/login 42 ABC-1 Fix login bug" }),
      row("devon/logout", null, 2, { search: "devon/logout 43 ABC-2 Fix logout" }),
      row("devon/docs", null, 3, { search: "devon/docs" }),
    ];
    const view = (q: string) => ids(layout(rows, state({ ...flatTable, q })));
    expect(view("LOGIN")).toEqual(["devon/login"]);
    expect(view("fix")).toEqual(["devon/login", "devon/logout"]);
    expect(view("fix logout")).toEqual(["devon/logout"]);
    expect(view("#43")).toEqual(["devon/logout"]);
    expect(view("abc-1")).toEqual(["devon/login"]);
    expect(view("nothing")).toEqual([]);
  });

  test("the next column floats rows that need the user first and filters by who", () => {
    const rows = [
      row("a", null, 1, { next: { value: "others", sort: "3" } }),
      row("b", null, 2, { next: { value: "you", sort: "1" } }),
      row("c", null, 3, { next: { value: "ci", sort: "2" } }),
      row("d", null, 4, { next: { value: "you", sort: "1" } }),
      row("e", null, 5, { next: { value: "", sort: "" } }),
    ];
    const view = (o: Partial<ViewState>) => ids(layout(rows, state({ ...flatTable, ...o })));
    expect(view({ sort: "next" })).toEqual(["b", "d", "c", "a", "e"]);
    expect(view({ next: "you" })).toEqual(["b", "d"]);
  });
});

describe("the tree layout", () => {
  // a ── a1, a2
  // b
  // The trunk is not a row: a stack's first branch has no parent row.
  const tree = [
    row("a", null, 1, { pr: { value: "open", sort: "30" } }),
    row("a1", "a", 2, { pr: { value: "open", sort: "5" } }),
    row("a2", "a", 3, { pr: { value: "closed", sort: "50" } }),
    row("b", null, 4, { pr: { value: "open", sort: "10" } }),
  ];

  test("is depth-first from each stack's first branch", () => {
    const out = layout(tree, state({ view: "table" }));
    expect(out.map((e) => [e.id, e.depth])).toEqual([
      ["a", 0],
      ["a1", 1],
      ["a2", 1],
      ["b", 0],
    ]);
  });

  test("applies the sort among siblings, keeping children under parents", () => {
    expect(ids(layout(tree, state({ view: "table", sort: "pr" })))).toEqual(["b", "a", "a1", "a2"]);
    expect(ids(layout(tree, state({ view: "table", sort: "pr", dir: "desc" })))).toEqual(["a", "a2", "a1", "b"]);
  });

  test("keeps the ancestors of matches for context, flagged", () => {
    const out = layout(tree, state({ view: "table", pr: "closed" }));
    expect(out.map((e) => [e.id, e.depth, e.context])).toEqual([
      ["a", 0, true],
      ["a2", 1, false],
    ]);
  });

  test("with no matches shows nothing", () => {
    expect(layout(tree, state({ view: "table", q: "zzz" }))).toEqual([]);
  });

  test("makes rows whose parent is missing roots, and does not hang on cycles", () => {
    const rows = [row("x", "gone", 0), row("y", "z", 1), row("z", "y", 2)];
    expect(ids(layout(rows, state({ view: "table" })))).toEqual(["x"]);
  });
});

describe("several repos", () => {
  // Ids are unique across repos, so a branch name can repeat (here "x").
  const multi = [
    row("1:x", null, 1, { repo: "1", pr: { value: "open", sort: "9" } }),
    row("1:y", null, 2, { repo: "1", pr: { value: "open", sort: "3" } }),
    row("2:x", null, 4, { repo: "2", pr: { value: "merged", sort: "1" } }),
  ];
  for (const r of multi) r.cells.repo = { value: r.repo, sort: r.repo === "1" ? "alpha" : "beta" };
  const table = state({ view: "table" });

  test("each repo's rows stay together in server order", () => {
    const out = layout(multi, table);
    expect(ids(out)).toEqual(["1:x", "1:y", "2:x"]);
    expect(out.map((e) => e.depth)).toEqual([0, 0, 0]);
  });

  test("sorting a column never interleaves repos in the tree", () => {
    expect(ids(layout(multi, { ...table, sort: "pr" }))).toEqual(["1:y", "1:x", "2:x"]);
  });

  test("sorting by repo orders the repo groups; the flat view sorts across repos", () => {
    expect(ids(layout(multi, { ...table, sort: "repo", dir: "desc" }))).toEqual(["2:x", "1:x", "1:y"]);
    expect(ids(layout(multi, state({ ...flatTable, sort: "pr" })))).toEqual(["2:x", "1:y", "1:x"]);
  });

  test("the repo filter scopes the view to one repo, and combines with value filters", () => {
    expect(ids(layout(multi, { ...table, repo: "2" }))).toEqual(["2:x"]);
    expect(ids(layout(multi, state({ ...flatTable, repo: "1" })))).toEqual(["1:x", "1:y"]);
    expect(ids(layout(multi, { ...table, repo: "1", pr: "open" }))).toEqual(["1:x", "1:y"]);
    expect(layout(multi, { ...table, repo: "2", pr: "open" })).toEqual([]);
  });
});

describe("done branches", () => {
  const withNext = (value: string) => ({ next: { value, sort: "" } });

  test("are hidden unless included or asked for by the next filter", () => {
    const rows = [
      row("a", null, 1, withNext("you")),
      row("b", null, 2, withNext("none")),
      row("c", null, 3, withNext("")),
    ];
    const view = (o: Partial<ViewState>) => ids(layout(rows, state({ ...flatTable, ...o })));
    expect(view({})).toEqual(["a", "c"]);
    expect(view({ done: "1" })).toEqual(["a", "b", "c"]);
    expect(view({ next: "none" })).toEqual(["b"]);
    expect(view({ next: "you" })).toEqual(["a"]);
  });

  test("a done parent stays as context for an open child", () => {
    const rows = [row("a", null, 1, withNext("none")), row("b", "a", 2, withNext("you"))];
    const out = layout(rows, state({ view: "table" }));
    expect(out.map((e) => [e.id, e.depth, e.context])).toEqual([
      ["a", 0, true],
      ["b", 1, false],
    ]);
    expect(matches(rows[0], state())).toBe(false);
  });
});

describe("the card views", () => {
  const nextRow = (id: string, parent: string | null, order: number, who: string, extra: Parameters<typeof row>[3] = {}) =>
    row(id, parent, order, { next: { value: who, sort: "" }, ...extra });

  test("grouped cards are sections by next, in order, empty ones omitted", () => {
    const rows = [
      nextRow("a", null, 1, "others"),
      nextRow("b", null, 2, "you"),
      nextRow("c", null, 3, "ci"),
      nextRow("d", null, 4, "you"),
      nextRow("e", null, 5, "none"),
    ];
    const sections = (o: Partial<ViewState> = {}) => groupedLayout(rows, state({ view: "grouped", ...o }));

    const out = sections();
    expect(out.map((s) => s.key)).toEqual(["you", "ci", "others"]);
    expect(out.map((s) => s.label)).toEqual(["Needs me", "Waiting on CI", "Waiting on others / team"]);
    expect(out.every((s) => s.meaning.length > 0)).toBe(true);
    expect(ids(out[0].entries)).toEqual(["b", "d"]);
    // Done is hidden unless included; filters apply as in the table.
    expect(sections({ done: "1" }).map((s) => s.key)).toEqual(["you", "ci", "others", "none"]);
    expect(sections({ next: "you" }).map((s) => s.key)).toEqual(["you"]);
    expect(out.every((s) => s.entries.every((e) => e.depth === 0 && !e.context))).toBe(true);
    // "Needs me" is first: sections are in the order of NEXT_SECTIONS.
    expect(NEXT_SECTIONS[0].key).toBe("you");
  });

  test("within a grouped section branches follow stack order; the sort applies among siblings", () => {
    const rows = [
      nextRow("1:a", null, 1, "you", { repo: "1", comments: { value: "x", sort: "5" } }),
      nextRow("1:b", "1:a", 2, "you", { repo: "1", comments: { value: "x", sort: "1" } }),
      nextRow("1:c", null, 3, "you", { repo: "1", comments: { value: "x", sort: "2" } }),
      nextRow("2:d", null, 4, "you", { repo: "2", comments: { value: "x", sort: "0" } }),
    ];

    const def = groupedLayout(rows, state({ view: "grouped" }));
    expect(ids(def[0].entries)).toEqual(["1:a", "1:b", "1:c", "2:d"]);

    const sorted = groupedLayout(rows, state({ view: "grouped", sort: "comments" }));
    // Roots by comments (c before a); a's child stays right under it.
    expect(ids(sorted[0].entries)).toEqual(["1:c", "1:a", "1:b", "2:d"]);
  });

  test("a branch whose next is not known yet is in a trailing Loading section", () => {
    const rows = [nextRow("a", null, 1, ""), nextRow("b", null, 2, "you")];
    const out = groupedLayout(rows, state({ view: "grouped" }));
    expect(out.map((s) => s.key)).toEqual(["you", "loading"]);
  });

  test("a finished parent is a slim stub above its children in grouped too, once per section", () => {
    const rows = [
      nextRow("1:a", null, 1, "none", { repo: "1" }),
      nextRow("1:b", "1:a", 2, "you", { repo: "1" }),
      nextRow("1:c", "1:a", 3, "you", { repo: "1" }),
      nextRow("1:d", "1:a", 4, "others", { repo: "1" }),
    ];

    const out = groupedLayout(rows, state({ view: "grouped" }));

    expect(out.map((s) => s.key)).toEqual(["you", "others"]);
    expect(out[0].entries.map((e) => [e.id, e.context])).toEqual([
      ["1:a", true],
      ["1:b", false],
      ["1:c", false],
    ]);
    // Each section that needs the context has its own stub.
    expect(out[1].entries.map((e) => [e.id, e.context])).toEqual([
      ["1:a", true],
      ["1:d", false],
    ]);
    // With done included the parent is a card of its own, in Done.
    const done = groupedLayout(rows, state({ view: "grouped", done: "1" }));
    expect(done.find((s) => s.key === "none")?.entries[0].context).toBe(false);
    expect(done.every((s) => s.entries.every((e) => !e.context))).toBe(true);
  });

  test("a chain of finished parents is stubbed outermost first", () => {
    const rows = [
      nextRow("a", null, 1, "none"),
      nextRow("b", "a", 2, "none"),
      nextRow("c", "b", 3, "you"),
    ];

    const out = groupedLayout(rows, state({ view: "grouped" }));

    expect(out[0].entries.map((e) => [e.id, e.context])).toEqual([
      ["a", true],
      ["b", true],
      ["c", false],
    ]);
  });

  test("stacked cards keep each repo's stacks together, indented by depth, finished parents as context", () => {
    const rows = [
      nextRow("1:a", null, 1, "none", { repo: "1" }),
      nextRow("1:b", "1:a", 2, "you", { repo: "1" }),
      nextRow("2:c", null, 3, "you", { repo: "2" }),
      nextRow("1:d", null, 4, "ci", { repo: "1" }),
    ];

    const out = stackedLayout(rows, state({ view: "stacked" }));

    expect(out.map((s) => s.key)).toEqual(["repo:1", "repo:2"]);
    expect(out[0].entries.map((e) => [e.id, e.depth, e.context])).toEqual([
      ["1:a", 0, true],
      ["1:b", 1, false],
      ["1:d", 0, false],
    ]);
    // With done included the parent is a card of its own.
    const done = stackedLayout(rows, state({ view: "stacked", done: "1" }));
    expect(done[0].entries[0].context).toBe(false);
  });

  test("rails: tees for siblings that continue, corners for the last, lines through subtrees", () => {
    // a
    //   b
    //     c
    //   d
    //     e
    const guides = railGuides([0, 1, 2, 1, 2].map((depth) => ({ depth })));

    expect(guides[0]).toEqual([]);
    expect(guides[1]).toEqual([{ col: 0, kind: "tee" }]);
    // c: b has a later sibling (d), so column 0 passes through; c is last under b.
    expect(guides[2]).toEqual([
      { col: 0, kind: "line" },
      { col: 1, kind: "corner" },
    ]);
    expect(guides[3]).toEqual([{ col: 0, kind: "corner" }]);
    // e: d is the last of a's children: no line in column 0.
    expect(guides[4]).toEqual([{ col: 1, kind: "corner" }]);
  });
});
