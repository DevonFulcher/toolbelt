import { describe, expect, test } from "vitest";
import { linkSegments } from "./linkSegments";

describe("linkSegments", () => {
  test("plain text is one segment", () => {
    expect(linkSegments("nothing to link")).toEqual([{ text: "nothing to link" }]);
  });

  test("a markdown link keeps its text and target", () => {
    expect(linkSegments("see [#12](https://github.com/o/r/pull/12) now")).toEqual([
      { text: "see " },
      { text: "#12", href: "https://github.com/o/r/pull/12" },
      { text: " now" },
    ]);
  });

  test("a bare URL drops trailing punctuation", () => {
    expect(linkSegments("at https://example.com/a, then")).toEqual([
      { text: "at " },
      { text: "https://example.com/a", href: "https://example.com/a" },
      { text: ", then" },
    ]);
  });

  test("only http and https become links", () => {
    expect(linkSegments("[x](javascript:alert(1))")).toEqual([{ text: "[x](javascript:alert(1))" }]);
  });
});
