import { describe, expect, test } from "vitest";
import type { DifftasticFile } from "../api/generated";
import crlf from "../testing/difftastic/crlf.json";
import crlfNew from "../testing/difftastic/crlf.new.py?raw";
import simple from "../testing/difftastic/simple.json";
import simpleNew from "../testing/difftastic/simple.new.py?raw";
import simpleOld from "../testing/difftastic/simple.old.py?raw";
import { buildChanges } from "./diffChanges";
import { changedSpansByLine, splitLines, utf8ColumnToUtf16 } from "./difftastic";

// Real `difft --display json` output for the `.py` pairs beside the JSON.
const simpleFile = simple as unknown as DifftasticFile;
const crlfFile = crlf as unknown as DifftasticFile;

describe("utf8ColumnToUtf16", () => {
  test("passes ASCII through", () => {
    expect(utf8ColumnToUtf16("abc", 2)).toBe(2);
  });

  test("counts a 2-byte character as one unit and an astral one as two", () => {
    expect(utf8ColumnToUtf16("é🎉x", 2)).toBe(1);
    expect(utf8ColumnToUtf16("é🎉x", 6)).toBe(3);
  });
});

describe("changedSpansByLine", () => {
  test("puts a span after non-ASCII text on the right UTF-16 range", () => {
    const lines = splitLines(simpleNew);
    const index = lines.findIndex((l) => l.includes("héllo"));
    const span = changedSpansByLine(simpleFile, "rhs", lines).get(index)!.at(-1)!;
    expect(lines[index].slice(span.from, span.to)).toBe('"héllo 🎉 wörld!"');
  });

  test("handles CRLF files", () => {
    const lines = splitLines(crlfNew);
    const [plus, one] = changedSpansByLine(crlfFile, "rhs", lines).get(1)!;
    expect(lines[1].slice(plus.from, plus.to)).toBe("+");
    expect(lines[1].slice(one.from, one.to)).toBe("1");
  });
});

describe("buildChanges", () => {
  test("keeps both sides' ranges increasing, as the merge view needs", () => {
    const changes = buildChanges(simpleFile, simpleOld, simpleNew);
    expect(changes.length).toBeGreaterThan(1);
    changes.slice(1).forEach((after, i) => {
      expect(after.fromA).toBeGreaterThanOrEqual(changes[i].toA);
      expect(after.fromB).toBeGreaterThanOrEqual(changes[i].toB);
    });
  });

  test("a created file is one insertion", () => {
    const created: DifftasticFile = { language: "Python", path: "a.py", status: "created" };
    const [change] = buildChanges(created, "", "x = 1\n");
    expect([change.fromA, change.toA, change.fromB, change.toB]).toEqual([0, 0, 0, 6]);
  });

  test("a deleted file is one deletion", () => {
    const deleted: DifftasticFile = { language: "Python", path: "a.py", status: "deleted" };
    const [change] = buildChanges(deleted, "x = 1\n", "");
    expect([change.fromA, change.toA, change.fromB, change.toB]).toEqual([0, 6, 0, 0]);
  });
});
