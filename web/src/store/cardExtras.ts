// What a task card shows beyond its PR status (the autonomous agents' view of
// the branch and what the task has cost), kept apart from the snapshot store:
// it comes from its own endpoint, asked again every few seconds while the page
// is visible and after anything that changes it.
import { useEffect } from "react";
import { create } from "zustand";
import { fetchCardExtras } from "../api/client";
import type { BranchExtras } from "../api/generated";

/** How often the extras are asked for again while the page is visible. */
export const EXTRAS_REFRESH_MS = 10_000;

interface CardExtrasState {
  byId: Record<string, BranchExtras>;
  /** When (ms) the agents last decided every task, and spend was last read
   * from the session transcripts: how old the Agent and Spend boxes' data is
   * (not when this page asked); null before the first time. */
  decidedAt: number | null;
  spendReadAt: number | null;
  set(branches: BranchExtras[], times?: { decidedAt: number | null; spendReadAt: number | null }): void;
}

export const useCardExtras = create<CardExtrasState>((set) => ({
  byId: {},
  decidedAt: null,
  spendReadAt: null,
  set: (branches, times = { decidedAt: null, spendReadAt: null }) =>
    set({ byId: Object.fromEntries(branches.map((branch) => [branch.id, branch])), ...times }),
}));

const isExtras = (value: unknown): value is BranchExtras =>
  typeof value === "object" && value !== null && "agent" in value && "spend" in value;

/** Ask for the extras now. A response that is not extras (or no response) is ignored. */
export async function refreshCardExtras(fetchImpl: typeof fetch = fetch): Promise<void> {
  try {
    const { branches, agents_decided_at: decided, spend_read_at: spend } = await fetchCardExtras(fetchImpl);
    if (Array.isArray(branches) && branches.every(isExtras)) {
      useCardExtras.getState().set(branches, {
        decidedAt: decided ? Date.parse(decided) : null,
        spendReadAt: spend ? Date.parse(spend) : null,
      });
    }
  } catch {
    // Cards show their agent and spend slots as unavailable until it works.
  }
}

/** Keeps the extras current while the component using it is mounted. */
export function useCardExtrasPolling(fetchImpl: typeof fetch = fetch): void {
  useEffect(() => {
    void refreshCardExtras(fetchImpl);
    const timer = window.setInterval(() => {
      if (!document.hidden) void refreshCardExtras(fetchImpl);
    }, EXTRAS_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [fetchImpl]);
}
