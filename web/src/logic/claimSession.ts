import type { BranchClaim, Link } from "../api/generated";

const STATE_ORDER = ["working", "idle", "blocked"] as const;

/**
 * The session link a card's "Interactive session" slot opens: the session of the
 * agent that holds the branch now (a working holder first), else the session the
 * branch was created in (`stored`).
 */
export function sessionLink(claim: BranchClaim, stored: Link | null): Link | null {
  for (const state of STATE_ORDER) {
    const holder = claim.holders.find((h) => h.kind === "agent" && h.state === state && h.link);
    if (holder?.link) return holder.link;
  }
  return stored;
}
