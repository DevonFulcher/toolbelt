import { useMemo } from "react";
import type { LiveStatus } from "../api/generated";
import { hiddenAsDone, inScope, matches, prioritiesLayout } from "../logic/layout";
import { rowOf, type Row } from "../logic/rows";
import { useApp } from "./StoreContext";
import type { AppState } from "./appStore";

export const LOADING: LiveStatus = { load: "loading" };

/** Every branch as a sortable / filterable row, in the server's order. */
export function useRows(): Row[] {
  const branches = useApp((s) => s.branches);
  const statuses = useApp((s) => s.statuses);
  const tasks = useApp((s) => s.tasks);
  const reposById = useApp((s) => s.reposById);
  return useMemo(
    () =>
      branches.map((branch) =>
        rowOf(branch, reposById[branch.repo_id], statuses[branch.id] ?? LOADING, tasks[branch.id] ?? null),
      ),
    [branches, statuses, tasks, reposById],
  );
}

export interface Counts {
  /** Branches passing every filter. */
  visible: number;
  /** Branches in the chosen repo (all, if none). */
  inRepo: number;
  /** Of those, how many are hidden only for being done. */
  doneHidden: number;
}

export function useCounts(rows: Row[]): Counts {
  const view = useApp((s) => s.view);
  return useMemo(() => {
    const inRepo = rows.filter((row) => inScope(row, view));
    return {
      visible: view.view === "priorities"
        ? prioritiesLayout(rows, view).reduce((total, section) => total + section.entries.length, 0)
        : rows.filter((row) => matches(row, view)).length,
      inRepo: inRepo.length,
      doneHidden: inRepo.filter((row) => hiddenAsDone(row, view)).length,
    };
  }, [rows, view]);
}

/** `id` and every branch stacked below it, in no particular order. */
export function subtreeIds(state: Pick<AppState, "branchesById">, id: string): string[] {
  const seen = new Set<string>([id]);
  const pending = [...(state.branchesById[id]?.children ?? [])];
  while (pending.length) {
    const child = pending.pop();
    if (!child || seen.has(child.id)) continue;
    seen.add(child.id);
    pending.push(...(state.branchesById[child.id]?.children ?? []));
  }
  return [...seen];
}
