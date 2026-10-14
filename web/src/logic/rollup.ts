import type { LiveStatus } from "../api/generated";

/** What a collapsed card stands for: itself and everything stacked below it. */
export interface Rollup {
  /** The card and its descendants. */
  total: number;
  /** Branches that need the user, by what they need (first seen first). */
  you: { label: string; n: number }[];
  /** Branches claimed by an agent (or a person). */
  agent: number;
  /** Branches an autonomous agent holds. */
  auto: number;
  /** Branches waiting on CI. */
  ci: number;
  /** Branches waiting on someone else. */
  others: number;
  /** Merged or closed branches. */
  done: number;
  /** Branches whose status is not known yet. */
  loading: number;
}

/**
 * Summarises `ids` (a card and its descendants) by who each is waiting on, so
 * a collapsed card can still say whether anything below it needs attention.
 * Every id lands in exactly one bucket, so the buckets add up to `total`.
 */
export function rollup(ids: readonly string[], statusOf: (id: string) => LiveStatus | undefined): Rollup {
  const out: Rollup = { total: ids.length, you: [], agent: 0, auto: 0, ci: 0, others: 0, done: 0, loading: 0 };
  for (const id of ids) {
    const status = statusOf(id);
    if (!status || status.load !== "loaded") {
      out.loading += 1;
      continue;
    }
    const { who, label } = status.next;
    if (who === "you") {
      const seen = out.you.find((entry) => entry.label === label);
      if (seen) seen.n += 1;
      else out.you.push({ label, n: 1 });
    } else if (who === "agent") out.agent += 1;
    else if (who === "auto") out.auto += 1;
    else if (who === "ci") out.ci += 1;
    else if (who === "others") out.others += 1;
    else out.done += 1;
  }
  return out;
}

/** "Merge, Resolve comments ×2": the labels of what needs the user. */
export function youText(entries: Rollup["you"]): string {
  return entries.map(({ label, n }) => (n > 1 ? `${label} ×${n}` : label)).join(", ");
}
