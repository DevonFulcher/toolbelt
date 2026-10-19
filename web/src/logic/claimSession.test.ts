import { describe, expect, test } from "vitest";
import type { BranchClaim, ClaimHolder, Link } from "../api/generated";
import { sessionLink } from "./claimSession";

const link = (href: string): Link => ({ label: "Open session", href });
const holder = (kind: ClaimHolder["kind"], state: ClaimHolder["state"], href: string | null): ClaimHolder =>
  ({ holder: `${kind}-${state}`, kind, state, link: href ? link(href) : null }) as ClaimHolder;
const claim = (...holders: ClaimHolder[]): BranchClaim => ({ holders });

describe("sessionLink", () => {
  const stored = link("claude://stored");

  test("uses the stored link when nothing holds the branch", () => {
    expect(sessionLink(claim(), stored)).toBe(stored);
    expect(sessionLink(claim(), null)).toBeNull();
  });

  test("prefers the live agent's session over the one the branch was created in", () => {
    expect(sessionLink(claim(holder("agent", "working", "claude://live")), stored)).toEqual(link("claude://live"));
  });

  test("takes a working holder before an idle or blocked one", () => {
    const holders = claim(
      holder("agent", "idle", "claude://idle"),
      holder("agent", "blocked", "claude://blocked"),
      holder("agent", "working", "claude://working"),
    );
    expect(sessionLink(holders, stored)).toEqual(link("claude://working"));
  });

  test("ignores holders that are not agents or have no session", () => {
    const holders = claim(holder("human", "working", "claude://human"), holder("agent", "working", null));
    expect(sessionLink(holders, stored)).toBe(stored);
  });
});
