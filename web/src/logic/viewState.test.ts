import { describe, expect, test } from "vitest";
import {
  COLUMNS,
  DEFAULT_STATE,
  initialState,
  parseState,
  serializeState,
  toggleSort,
  VALUE_FILTERS,
  VIEWS,
  type ViewState,
} from "./viewState";

const state = (overrides: Partial<ViewState> = {}): ViewState => ({ ...DEFAULT_STATE, ...overrides });

describe("the URL state", () => {
  test("round-trips through the query string and omits defaults", () => {
    expect(serializeState(DEFAULT_STATE)).toBe("");
    const full = state({
      view: "table",
      mode: "flat",
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
    expect(parseState(serializeState(full))).toEqual(full);
    // Direction is only meaningful with a sort column.
    expect(serializeState(state({ dir: "desc" }))).toBe("");
  });

  test("grouped is the default view, so it is the bare URL", () => {
    expect(DEFAULT_STATE.view).toBe("grouped");
    expect(serializeState(state({ view: "grouped" }))).toBe("");
    expect(serializeState(state({ view: "stacked" }))).toBe("?view=stacked");
    expect(serializeState(state({ view: "table" }))).toBe("?view=table");
  });

  test("table-only parameters are not written for the card views", () => {
    expect(serializeState(state({ view: "stacked", mode: "flat", compact: "1" }))).toBe("?view=stacked");
    expect(serializeState(state({ view: "table", mode: "flat" }))).toBe("?view=table&mode=flat");
    expect(serializeState(state({ view: "table", compact: "1" }))).toBe("?view=table&compact=1");
  });

  test("the focus root is written only for the views that draw a tree", () => {
    const root = "1:devon/a";
    expect(serializeState(state({ view: "stacked", root }))).toBe("?view=stacked&root=1%3Adevon%2Fa");
    expect(serializeState(state({ view: "table", root }))).toBe("?view=table&root=1%3Adevon%2Fa");
    expect(serializeState(state({ view: "table", mode: "flat", root }))).toBe("?view=table&mode=flat");
    expect(serializeState(state({ view: "grouped", root }))).toBe("");
    expect(parseState("?view=stacked&root=1%3Adevon%2Fa").root).toBe(root);
    expect(parseState("?view=stacked").root).toBe("");
  });

  test("ignores unknown sort columns and view names", () => {
    const parsed = parseState("?sort=evil&view=3d&dir=sideways&done=yes&group=x");
    expect(parsed).toEqual(DEFAULT_STATE);
  });

  test("the retired timeline view and its filters are ignored, not an error", () => {
    const parsed = parseState("?view=timeline&kind=review&order=oldest&next=you");
    expect(parsed).toEqual({ ...DEFAULT_STATE, next: "you" });
    expect(serializeState(parsed)).toBe("?next=you");
    expect(VIEWS).toEqual(["table", "stacked", "grouped", "aggregate"]);
  });

  test("old links map onto the current views", () => {
    // view=cards is grouped; view=tree|flat the table; group is ignored.
    expect(parseState("?view=cards").view).toBe("grouped");
    expect(parseState("?view=cards&group=repo")).toEqual(state({ view: "grouped" }));
    expect(parseState("?view=tree")).toEqual(state({ view: "table", mode: "tree" }));
    expect(parseState("?view=flat")).toEqual(state({ view: "table", mode: "flat" }));
    // A bare mode or compact (which only the table has) meant the table when
    // the table was the default view.
    expect(parseState("?mode=flat")).toEqual(state({ view: "table", mode: "flat" }));
    expect(parseState("?compact=1")).toEqual(state({ view: "table", compact: "1" }));
    // ...but not next to a view that says otherwise.
    expect(parseState("?view=stacked&mode=flat").view).toBe("stacked");
  });

  test("the card views live in the URL", () => {
    for (const view of ["stacked", "grouped"] as const) {
      expect(parseState(serializeState(state({ view }))).view).toBe(view);
    }
    const full = state({ view: "stacked", q: "auth", next: "you", done: "1", sort: "ci", dir: "desc" });
    expect(parseState(serializeState(full))).toEqual(full);
  });

  test("compact comes from the URL, else from the remembered choice", () => {
    expect(initialState("", null).compact).toBe("");
    expect(initialState("", "1").compact).toBe("1");
    expect(initialState("?view=table&compact=1", null).compact).toBe("1");
    expect(initialState("?view=table", "1").compact).toBe("1");
    expect(initialState("?view=table", "0").compact).toBe("");
  });

  test("the comments, repo and next columns are sortable and filterable", () => {
    for (const column of ["comments", "repo", "next"] as const) {
      expect(COLUMNS).toContain(column);
    }
    expect(VALUE_FILTERS).toEqual(["next", "pr", "ci", "review", "comments", "merge"]);
    expect(parseState("?next=you").next).toBe("you");
  });
});

test("toggleSort starts ascending and flips on the same column", () => {
  let s = toggleSort(state(), "pr");
  expect([s.sort, s.dir]).toEqual(["pr", "asc"]);
  s = toggleSort(s, "pr");
  expect([s.sort, s.dir]).toEqual(["pr", "desc"]);
  s = toggleSort(s, "ci");
  expect([s.sort, s.dir]).toEqual(["ci", "asc"]);
});

describe("the aggregate view's state", () => {
  test("is a view of its own, with a 12 week window by default", () => {
    expect(VIEWS).toContain("aggregate");
    expect(parseState("?view=aggregate")).toMatchObject({ view: "aggregate", weeks: 12 });
    expect(serializeState(state({ view: "aggregate" }))).toBe("?view=aggregate");
  });

  test("carries a non-default window in the URL, and only for that view", () => {
    expect(serializeState(state({ view: "aggregate", weeks: 26 }))).toBe("?view=aggregate&weeks=26");
    expect(parseState("?view=aggregate&weeks=52").weeks).toBe(52);
    expect(parseState("?view=aggregate").weekends).toBe("");
    expect(parseState("?view=aggregate&weekends=exclude").weekends).toBe("exclude");
    expect(parseState("?view=aggregate&weekends=nope").weekends).toBe("");
    expect(serializeState(state({ view: "aggregate", weekends: "exclude" }))).toBe(
      "?view=aggregate&weekends=exclude",
    );
    // Only for the aggregate view.
    expect(serializeState(state({ view: "table", weekends: "exclude" }))).toBe("?view=table");
    // Another view's URL does not keep it.
    expect(serializeState(state({ view: "table", weeks: 26 }))).toBe("?view=table");
  });

  test("ignores a window that is not offered", () => {
    expect(parseState("?view=aggregate&weeks=7").weeks).toBe(12);
    expect(parseState("?view=aggregate&weeks=abc").weeks).toBe(12);
  });
});
