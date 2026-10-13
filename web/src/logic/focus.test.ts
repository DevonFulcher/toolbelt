import { describe, expect, test } from "vitest";
import {
  ancestorIds,
  branchCountText,
  breadcrumbs,
  capEntries,
  focusCounts,
  focusedRows,
  focusSize,
  foldEntries,
  knownRoot,
  MAX_VISIBLE_DEPTH,
  stackViewRoot,
  upTarget,
} from "./focus";
import { layout, stackedLayout } from "./layout";
import type { Row } from "./rows";
import { DEFAULT_STATE, type ViewState } from "./viewState";

const state = (overrides: Partial<ViewState> = {}): ViewState => ({ ...DEFAULT_STATE, ...overrides });

function row(id: string, parent: string | null, order: number, repo = "1"): Row {
  return { id, repo, repoName: `repo${repo}`, branch: id, parent, order, search: id, cells: {} };
}

/** b0 <- b1 <- ... <- b(n-1), one stack. */
function chain(n: number): Row[] {
  return Array.from({ length: n }, (_, i) => row(`b${i}`, i ? `b${i - 1}` : null, i));
}

const ids = (items: { id: string }[]): string[] => items.map((i) => i.id);

describe("the focus root", () => {
  // r <- a <- a1, r <- b;  other <- o1 (a second stack)
  const rows = [
    row("r", null, 0),
    row("a", "r", 1),
    row("a1", "a", 2),
    row("b", "r", 3),
    row("other", null, 4),
    row("o1", "other", 5),
  ];

  test("focuses on a subtree, root included, in the given order", () => {
    expect(ids(focusedRows(rows, "a"))).toEqual(["a", "a1"]);
    expect(ids(focusedRows(rows, "r"))).toEqual(["r", "a", "a1", "b"]);
    expect(focusSize(rows, "r")).toBe(4);
  });

  test("an unknown or empty root is ignored", () => {
    expect(knownRoot(rows, "nope")).toBe("");
    expect(knownRoot(rows, "")).toBe("");
    expect(knownRoot(rows, "a")).toBe("a");
    expect(focusedRows(rows, "nope")).toBe(rows);
    expect(upTarget(rows, "nope")).toBe("");
    expect(breadcrumbs(rows, "nope")).toBeNull();
  });

  test("the tree layout counts depth from the focus root", () => {
    const entries = layout(rows, state({ view: "stacked", root: "a" }));

    expect(entries).toEqual([
      { id: "a", depth: 0, context: false },
      { id: "a1", depth: 1, context: false },
    ]);
    // Without it every stack is laid out from its root.
    expect(ids(layout(rows, state({ view: "stacked" })))).toEqual(["r", "a", "a1", "b", "other", "o1"]);
  });

  test("the flat table ignores it", () => {
    const entries = layout(rows, state({ view: "table", mode: "flat", root: "a" }));
    expect(ids(entries)).toEqual(["r", "a", "a1", "b", "other", "o1"]);
  });

  test("filters still apply inside the focus", () => {
    const found = layout(rows, state({ view: "stacked", root: "r", q: "a1" }));

    // The match, with its ancestors up to the root kept as context.
    expect(found).toEqual([
      { id: "r", depth: 0, context: true },
      { id: "a", depth: 1, context: true },
      { id: "a1", depth: 2, context: false },
    ]);
  });

  test("up goes to the parent, or to the whole stack from a stack root", () => {
    expect(upTarget(rows, "a1")).toBe("a");
    expect(upTarget(rows, "a")).toBe("r");
    expect(upTarget(rows, "r")).toBe("");
  });

  test("ancestors run from the stack root to the parent, and cycles do not hang", () => {
    expect(ancestorIds(rows, "a1")).toEqual(["r", "a"]);
    expect(ancestorIds(rows, "r")).toEqual([]);
    const loop = [row("x", "y", 0), row("y", "x", 1)];
    expect(ancestorIds(loop, "x")).toEqual(["y"]);
  });
});

describe("what the filters leave of the focused subtree", () => {
  const rows = chain(5);

  test("counts the subtree's branches that pass, out of all of them", () => {
    expect(focusCounts(rows, "b1", (r) => r.id !== "b3")).toEqual({ shown: 3, total: 4 });
    expect(focusCounts(rows, "b1", () => true)).toEqual({ shown: 4, total: 4 });
    // Branches outside the subtree do not count, whether or not they pass.
    expect(focusCounts(rows, "b4", () => false)).toEqual({ shown: 0, total: 1 });
  });

  test("reads 'M branches', or 'N of M branches' when filters narrow it", () => {
    expect(branchCountText({ shown: 17, total: 17 })).toBe("17 branches");
    expect(branchCountText({ shown: 1, total: 1 })).toBe("1 branch");
    expect(branchCountText({ shown: 3, total: 17 })).toBe("3 of 17 branches");
    expect(branchCountText({ shown: 0, total: 2 })).toBe("0 of 2 branches");
  });
});

describe("where a branch's stack is rooted for the stacked view", () => {
  const parents: Record<string, string | null> = {};
  for (const r of chain(12)) parents[r.id] = r.parent;
  const parentOf = (id: string): string | null => parents[id] ?? null;

  test("a stack root is its own root", () => {
    expect(stackViewRoot("b0", parentOf)).toBe("b0");
  });

  test("is the stack root, so the whole stack is drawn around the branch", () => {
    expect(stackViewRoot("b3", parentOf)).toBe("b0");
    expect(stackViewRoot(`b${MAX_VISIBLE_DEPTH}`, parentOf)).toBe("b0");
  });

  test("moves down just far enough to keep a deep branch inside the depth cap", () => {
    // b11 is 11 below b0: rooted at b5 it is at the cap (6), not hidden below it.
    expect(stackViewRoot("b11", parentOf)).toBe("b5");
    const entries = capEntries(layout(chain(12), state({ view: "stacked", root: "b5" })));
    expect(entries[entries.length - 1]).toEqual({ id: "b11", depth: 6, context: false });
  });

  test("does not hang on a cycle", () => {
    const loop: Record<string, string> = { x: "y", y: "x" };
    expect(stackViewRoot("x", (id) => loop[id] ?? null)).toBe("y");
  });
});

describe("breadcrumbs", () => {
  test("list every ancestor when the chain is short", () => {
    const crumbs = breadcrumbs(chain(4), "b3");

    expect(crumbs?.repoName).toBe("repo1");
    expect(crumbs?.segments).toEqual([
      { kind: "branch", crumb: { id: "b0", name: "b0" } },
      { kind: "branch", crumb: { id: "b1", name: "b1" } },
      { kind: "branch", crumb: { id: "b2", name: "b2" } },
    ]);
    expect(crumbs?.current).toEqual({ id: "b3", name: "b3" });
  });

  test("collapse the middle of a long chain into a gap listing it", () => {
    const crumbs = breadcrumbs(chain(12), "b11");

    expect(crumbs?.segments.map((s) => (s.kind === "gap" ? "…" : s.crumb.id))).toEqual([
      "b0",
      "…",
      "b9",
      "b10",
    ]);
    const gap = crumbs?.segments[1];
    expect(gap?.kind === "gap" && gap.hidden.map((c) => c.id)).toEqual([
      "b1", "b2", "b3", "b4", "b5", "b6", "b7", "b8",
    ]);
  });

  test("a stack root has only the repo and itself", () => {
    expect(breadcrumbs(chain(3), "b0")?.segments).toEqual([]);
  });
});

describe("the depth cap", () => {
  test("a linear stack stops at the cap and says how much is below", () => {
    const capped = capEntries(layout(chain(20), state({ view: "stacked" })));

    expect(capped).toHaveLength(MAX_VISIBLE_DEPTH + 1);
    expect(capped[capped.length - 1]).toEqual({
      id: `b${MAX_VISIBLE_DEPTH}`,
      depth: MAX_VISIBLE_DEPTH,
      context: false,
      hidden: { levels: 13, branches: 13 },
    });
    // Nothing above the cap is marked.
    expect(capped.slice(0, -1).some((e) => e.hidden)).toBe(false);
  });

  test("a stack that fits is untouched", () => {
    const entries = layout(chain(MAX_VISIBLE_DEPTH + 1), state({ view: "stacked" }));
    expect(capEntries(entries)).toEqual(entries);
  });

  test("counts levels and branches below each capped branch separately", () => {
    // depth 2 cap: root <- x (<- y <- z) and x <- w
    const rows = [
      row("root", null, 0),
      row("x", "root", 1),
      row("y", "x", 2),
      row("z", "y", 3),
      row("w", "x", 4),
      row("late", "root", 5),
    ];
    const capped = capEntries(layout(rows, state({ view: "stacked" })), 1);

    expect(capped).toEqual([
      { id: "root", depth: 0, context: false },
      { id: "x", depth: 1, context: false, hidden: { levels: 2, branches: 3 } },
      { id: "late", depth: 1, context: false },
    ]);
  });

  test("re-rooting at a capped branch shows what was hidden, counted from there", () => {
    const rows = chain(20);
    const rooted = capEntries(layout(rows, state({ view: "stacked", root: `b${MAX_VISIBLE_DEPTH}` })));

    expect(rooted[0]).toEqual({ id: "b6", depth: 0, context: false });
    expect(rooted).toHaveLength(MAX_VISIBLE_DEPTH + 1);
    expect(rooted[rooted.length - 1].id).toBe("b12");
    expect(rooted[rooted.length - 1].hidden).toEqual({ levels: 7, branches: 7 });
  });

  test("the stacked sections are capped; rails stay right up to the cap", () => {
    const sections = stackedLayout(chain(20), state({ view: "stacked" }));

    expect(sections).toHaveLength(1);
    expect(sections[0].entries).toHaveLength(MAX_VISIBLE_DEPTH + 1);
  });
});

describe("folding (collapsing the descendants of a card)", () => {
  // a <- b <- c, and a <- d: a has descendants b, c, d.
  const tree = (): Row[] => [row("a", null, 0), row("b", "a", 1), row("c", "b", 2), row("d", "a", 3)];
  const entries = () => layout(tree(), state({ view: "stacked" }));

  test("hides everything below a folded card and says how many", () => {
    const folded = foldEntries(entries(), new Set(["a"]));

    expect(folded.map((e) => e.id)).toEqual(["a"]);
    expect(folded[0].folded).toEqual({ branches: 3 });
  });

  test("a folded child hides only its own subtree", () => {
    const folded = foldEntries(entries(), new Set(["b"]));

    expect(folded.map((e) => e.id)).toEqual(["a", "b", "d"]);
    expect(folded.find((e) => e.id === "b")!.folded).toEqual({ branches: 1 });
    expect(folded.find((e) => e.id === "a")!.folded).toBeUndefined();
  });

  test("folding a leaf, an unknown id or nothing changes nothing", () => {
    const base = entries();

    expect(foldEntries(base, new Set(["c"]))).toEqual(base);
    expect(foldEntries(base, new Set(["zzz"]))).toEqual(base);
    expect(foldEntries(base, new Set())).toBe(base);
  });

  test("a fold inside a folded subtree is moot, and the cap still applies after it", () => {
    const folded = foldEntries(entries(), new Set(["a", "b"]));

    expect(folded.map((e) => e.id)).toEqual(["a"]);
    expect(folded[0].folded).toEqual({ branches: 3 });
  });

  test("stackedLayout applies the folded ids", () => {
    const sections = stackedLayout(tree(), state({ view: "stacked" }), new Set(["a"]));

    expect(sections.flatMap((section) => section.entries.map((e) => e.id))).toEqual(["a"]);
  });
});
