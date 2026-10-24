import { describe, expect, it } from "vitest";
import { changedSpansByLine, splitLines, utf8ColumnToUtf16, type DifftasticFile } from "./difftastic";
import simple from "../fixtures/simple.json";
import crlf from "../fixtures/crlf.json";
import crlfNew from "../fixtures/crlf.new.py?raw";
import simpleNew from "../fixtures/simple.new.py?raw";

describe("utf8ColumnToUtf16", () => {
  it("passes ASCII through", () => {
    expect(utf8ColumnToUtf16("abc", 2)).toBe(2);
  });
  it("counts 2-byte chars as one unit and astral chars as two", () => {
    expect(utf8ColumnToUtf16("é🎉x", 2)).toBe(1);
    expect(utf8ColumnToUtf16("é🎉x", 6)).toBe(3);
  });
});

describe("changedSpansByLine", () => {
  it("lands the string span after non-ASCII on the right UTF-16 range", () => {
    const lines = splitLines(simpleNew);
    const spans = changedSpansByLine(simple as unknown as DifftasticFile, "rhs", lines);
    const stringLine = lines.findIndex((l) => l.includes("héllo"));
    const span = spans.get(stringLine)!.at(-1)!;
    expect(lines[stringLine].slice(span.from, span.to)).toBe('"héllo 🎉 wörld!"');
  });
  it("handles CRLF files", () => {
    const lines = splitLines(crlfNew);
    const spans = changedSpansByLine(crlf as unknown as DifftasticFile, "rhs", lines);
    const [plus, one] = spans.get(1)!;
    expect(lines[1].slice(plus.from, plus.to)).toBe("+");
    expect(lines[1].slice(one.from, one.to)).toBe("1");
  });
});
