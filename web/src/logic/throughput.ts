// Throughput: PRs opened, merged and closed per local day (4- and 12-week
// windows) or per local week (26 and 52 weeks), from the PRs' UTC instants and
// the viewer's time zone. Pure; the zone is injected.
import type { PrMilestones } from "../api/generated";
import { bucketIndex, localBuckets, type Bucket } from "./localTime";

export type Granularity = "day" | "week";

/** Daily bars up to 12 weeks (28 / 84 of them), weekly beyond (not 182 / 364). */
export function granularityFor(weeks: number): Granularity {
  return weeks <= 12 ? "day" : "week";
}

export interface Throughput {
  granularity: Granularity;
  buckets: Bucket[];
  opened: number[];
  merged: number[];
  /** Closed without merging. */
  closed: number[];
}

export function throughput(
  prs: readonly PrMilestones[],
  options: { windowStart: number; now: number; weeks: number; timeZone: string },
): Throughput {
  const granularity = granularityFor(options.weeks);
  const buckets = localBuckets(
    options.windowStart,
    options.now,
    granularity === "day" ? 1 : 7,
    options.timeZone,
  );
  const opened = buckets.map(() => 0);
  const merged = buckets.map(() => 0);
  const closed = buckets.map(() => 0);
  for (const pr of prs) {
    const open = bucketIndex(buckets, Date.parse(pr.opened));
    if (open >= 0) opened[open]++;
    if (pr.ended === null) continue;
    const end = bucketIndex(buckets, Date.parse(pr.ended));
    if (end < 0) continue;
    if (pr.state === "MERGED") merged[end]++;
    else if (pr.state === "CLOSED") closed[end]++;
  }
  return { granularity, buckets, opened, merged, closed };
}
