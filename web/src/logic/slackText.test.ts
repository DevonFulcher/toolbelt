import { describe, expect, test } from "vitest";
import type { SlackDraftPr } from "../api/generated";
import { prInfo, renameMention } from "./slackText";

const POST = [
  "- [One](https://github.com/o/r/pull/1) @alan `+1/-1`",
  "  - [Two](https://github.com/o/r/pull/12) @alan `+2/-2`",
  "- [Three](https://github.com/o/r/pull/123) @alan `+3/-3`",
].join("\n");

describe("renameMention", () => {
  test("renames the name on every line", () => {
    expect(renameMention(POST, "alan", "will")).toBe(POST.split("@alan").join("@will"));
  });

  test("renames it only on the lines of the PR given, whose number may start another's", () => {
    const renamed = renameMention(POST, "alan", "will", 12).split("\n");

    expect(renamed[0]).toContain("@alan");
    expect(renamed[1]).toContain("@will");
    expect(renamed[2]).toContain("@alan"); // pull/123 is not pull/12
  });

  test("does not rename a longer name that starts the same", () => {
    expect(renameMention("@alan.andradecestelos and @alan", "alan", "will")).toBe(
      "@alan.andradecestelos and @will",
    );
  });

  test("renames a name that ends a sentence", () => {
    expect(renameMention("Please look today @alan.", "alan", "will")).toBe("Please look today @will.");
  });

  test("names with dots are matched literally", () => {
    expect(renameMention("@daniel.baptista.dias @danielXbaptista", "daniel.baptista.dias", "will")).toBe(
      "@will @danielXbaptista",
    );
  });
});

describe("prInfo", () => {
  const pr = (over: Partial<SlackDraftPr> = {}): SlackDraftPr => ({
    repo: "o/r",
    number: 1,
    url: "https://github.com/o/r/pull/1",
    title: "One",
    additions: 1,
    deletions: 1,
    assignee: null,
    included: true,
    pr: "open",
    ci: "success",
    review: "requested",
    open_comments: 0,
    has_conflicts: false,
    next: { label: "Post in Slack", who: "you", kind: "post_slack", would_be: null },
    ...over,
  });

  test("a PR ready to post says its CI and review and nothing about its next action", () => {
    expect(prInfo(pr())).toEqual(["CI success", "review requested"]);
  });

  test("says what is wrong with it: draft, no review, comments, conflicts", () => {
    expect(prInfo(pr({ pr: "draft", ci: "failed", review: "none", open_comments: 1, has_conflicts: true }))).toEqual([
      "draft",
      "CI failed",
      "no review yet",
      "1 open comment",
      "merge conflicts",
    ]);
  });

  test("a PR an agent holds says what it would be doing otherwise", () => {
    const held = pr({
      next: {
        label: "Autonomous agent",
        who: "auto",
        kind: "autonomous",
        would_be: { label: "Post in Slack", who: "you", kind: "post_slack" },
      },
    });

    expect(prInfo(held).at(-1)).toBe("Autonomous agent (would be: Post in Slack)");
  });

  test("a PR whose status is not loaded has no next action to show", () => {
    expect(prInfo(pr({ next: null }))).toEqual(["CI success", "review requested"]);
  });
});
