import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

// A card is a wrapping flex row of boxes (Git, PR, Task) with the timeline
// across the whole row. It has no breakpoints: its boxes shrink instead, so
// these tests read styles.css and check the properties that make that true.
// (jsdom does no layout; the measurement in docs/ui-testing.md checks the
// result in a real browser.)
const css = readFileSync(resolve(__dirname, "styles.css"), "utf8");

/** The declarations of the rule whose selector is exactly `selector`. */
const declarations = (selector: string): string => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`(?:^|\\n|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`));
  if (!match) throw new Error(`styles.css: no rule for ${selector}`);
  return match[1];
};

describe("card layout", () => {
  test("a card wraps its boxes instead of switching layouts at a width", () => {
    const card = declarations(".card");

    expect(card).toMatch(/display:\s*flex/);
    expect(card).toMatch(/flex-wrap:\s*wrap/);
    expect(card).toMatch(/min-width:\s*0/);
    // A width-triggered re-layout is what the nine-column grid needed, and
    // what a card wider than its column came from.
    for (const block of css.match(/@media[^{]*\{[\s\S]*?\n\}/g) ?? []) {
      expect(block, "no media query may restyle the card or its boxes").not.toMatch(
        /\.(card|group|g-[a-z]+|slot|s-[a-z_]+)\b/,
      );
    }
  });

  test("the boxes of a row are as tall as the tallest, content at the top", () => {
    // `stretch` (not `start`/`center`) makes the Git, PR and Task boxes read as
    // one band, and each row of a wrapped card equal-height within itself.
    expect(declarations(".card")).toMatch(/align-items:\s*stretch/);
    // A box must not opt out of it, and its content is a block that starts
    // at the top (not a centred or bottom-justified flex column).
    expect(declarations(".card .group")).not.toMatch(/align-self|display:\s*flex|justify-content/);
    expect(css).not.toMatch(/\.g-(?:git|pr|task)[^{]*\{[^}]*align-self/);
  });

  test("the timeline's button row wraps, so it never makes the track wider than the card", () => {
    // Expand, Hide commits and ? beside the start and end labels did, at 400px
    // in a card indented six levels.
    expect(declarations(".tl-ends")).toMatch(/flex-wrap:\s*wrap/);
    expect(declarations(".tl-buttons")).toMatch(/flex-wrap:\s*wrap/);
  });

  test("a group can shrink below its preferred width, so it is never wider than the card", () => {
    const group = declarations(".card .group");

    // `flex: <grow> <shrink> <basis>`: shrink must be 1 (or more).
    expect(group).toMatch(/flex:\s*[\d.]+\s+[1-9][\d.]*\s+/);
    expect(group).toMatch(/min-width:\s*0/);
  });

  test("slots inside a group never force it wider: columns are capped by the group", () => {
    const grid = declarations(".group-slots");

    // minmax(min(7rem, 100%), 1fr): a one-column group is as wide as the group.
    expect(grid).toMatch(/repeat\(auto-fill,\s*minmax\(min\([\d.]+rem,\s*100%\),\s*1fr\)\)/);
    // A slot spans the group's whole row with `1 / -1`; `span N` would add
    // implicit columns to a group that has fewer.
    expect(css).not.toMatch(/\.s-[a-z_]+[^{}]*\{[^}]*grid-(?:column|area):[^;}]*span/);
  });

  test("the timeline takes the whole row and nothing outranks it", () => {
    const timeline = declarations(".card .g-timeline");

    expect(timeline).toMatch(/flex:\s*1 0 100%/);
    expect(timeline).toMatch(/min-width:\s*0/);
    // Every other flex rule that can reach the card's children is on `.group`
    // (selectors of the form `.card .group`): none may name the timeline.
    const flexRules = [...css.matchAll(/([^{}]+)\{([^}]*\bflex:[^}]*)\}/g)].filter(([, selector]) =>
      /\.card\b[^,]*\.(?:group|g-timeline|slot)\b/.test(selector),
    );
    for (const [, selector] of flexRules) {
      if (/\.g-timeline/.test(selector)) continue;
      expect(selector, "a rule that sets flex on the card's children").toMatch(/\.group\b/);
    }
  });
});
