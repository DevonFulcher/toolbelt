import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";
import { MAX_VISIBLE_DEPTH } from "./logic/focus";

// The card is a fixed nine-column grid above some viewport width and a
// wrapping flex row below it. The grid's width comes from its columns, so the
// breakpoints in styles.css have to be derived from them: a card wider than its
// column overflows (the track and the right-hand slots spill out of the card).
const css = readFileSync(resolve(__dirname, "styles.css"), "utf8");
const REM = 16;

const columns = (): number[] => {
  const match = css.match(/\.card\s*\{\s*--cols:\s*([^;]+);/);
  if (!match) throw new Error("styles.css: .card --cols not found");
  // Every track's minimum: `minmax(17rem, 1.6fr)` -> 17, `13rem` -> 13.
  return match[1].match(/(?:minmax\(\s*)?([\d.]+)rem/g)!.map((t) => parseFloat(t.replace(/[^\d.]/g, "")));
};

const gridRem = (): number => {
  const cols = columns();
  const gap = Number(css.match(/\.card\s*\{[^}]*gap:\s*\d+px\s+(\d+)px/)![1]) / REM;
  const pad = Number(css.match(/\.card\s*\{[^}]*padding:\s*\d+px\s+(\d+)px/)![1]) / REM;
  const mainPad = Number(css.match(/\nmain\s*\{[^}]*padding:\s*\d+px\s+(\d+)px/)![1]) / REM;
  const border = 2 / REM;
  return cols.reduce((a, b) => a + b, 0) + (cols.length - 1) * gap + 2 * pad + border + 2 * mainPad;
};

const breakpoint = (selectorStart: string): number => {
  const re = new RegExp(`@media \\(max-width: ([\\d.]+)rem\\)\\s*\\{\\s*${selectorStart}`);
  const match = css.match(re);
  if (!match) throw new Error(`styles.css: no max-width media query for ${selectorStart}`);
  return parseFloat(match[1]);
};

describe("card layout breakpoints", () => {
  test("the grid is only used where the viewport can hold its columns", () => {
    expect(breakpoint("\\.card \\{")).toBeGreaterThanOrEqual(gridRem());
  });

  test("stacked cards also leave room for the deepest indentation", () => {
    const step = parseFloat(css.match(/\.cards\s*\{\s*--step:\s*([\d.]+)rem/)![1]);

    expect(breakpoint("\\.cards-stacked \\.card \\{")).toBeGreaterThanOrEqual(
      gridRem() + MAX_VISIBLE_DEPTH * step,
    );
  });
});
