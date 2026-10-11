// Lead time over time: the per-PR milestones the server sends, turned into
// per-stage durations, bucketed by the day/week a PR merged in the viewer's
// time zone, with a median and percentiles per bucket and over the window.
//
// Pure functions over `PrMilestones` and an injected IANA time zone.
import type { PrMilestones } from "../api/generated";
import { bucketIndex, localBuckets, weekdaySeconds, type Bucket } from "./localTime";

export type StageKey =
  | "start_to_open"
  | "open_to_request"
  | "request_to_review"
  | "review_to_merge"
  | "start_to_merge";

/** The stages in the order of a PR's life; the last is the whole span. */
export const STAGES: readonly { key: StageKey; label: string }[] = [
  { key: "start_to_open", label: "Task started to PR opened" },
  { key: "open_to_request", label: "PR opened to review requested" },
  { key: "request_to_review", label: "Review requested to first review" },
  { key: "review_to_merge", label: "First review to merged" },
  { key: "start_to_merge", label: "Task started to merged" },
];

export const TOTAL_STAGE: StageKey = "start_to_merge";

/** Which milestones a stage runs between (`null`: the milestone is unknown). */
function endpoints(pr: PrMilestones, key: StageKey): [string | null, string | null] {
  switch (key) {
    case "start_to_open":
      return [pr.task_started, pr.opened];
    case "open_to_request":
      return [pr.opened, pr.first_review_requested];
    case "request_to_review":
      return [pr.first_review_requested, pr.first_review_after_request];
    case "review_to_merge":
      return [pr.first_review, pr.ended];
    case "start_to_merge":
      return [pr.task_started, pr.ended];
  }
}

export interface DurationOptions {
  /** Count only the time that falls on Monday to Friday (see `weekdaySeconds`). */
  excludeWeekends: boolean;
  timeZone: string;
}

/** A stage's duration of `pr` in seconds, or `null` when unknown. */
export function stageSeconds(pr: PrMilestones, key: StageKey, options: DurationOptions): number | null {
  const [from, to] = endpoints(pr, key);
  if (from === null || to === null) return null;
  const a = Date.parse(from);
  const b = Date.parse(to);
  if (options.excludeWeekends) return weekdaySeconds(a, Math.max(a, b), options.timeZone);
  return Math.max((b - a) / 1000, 0);
}

/**
 * The `fraction` quantile of ascending `sorted` by linear interpolation
 * between the closest ranks (the "type 7" definition: the median of 1, 2, 3,
 * 4 is 2.5); `null` when empty. Used for the median, p75 and p90 alike.
 */
export function percentile(sorted: readonly number[], fraction: number): number | null {
  if (sorted.length === 0) return null;
  const position = (sorted.length - 1) * fraction;
  const low = Math.floor(position);
  const high = Math.ceil(position);
  return sorted[low] + (sorted[high] - sorted[low]) * (position - low);
}

export interface Stats {
  /** PRs with the stage known / unknown. */
  n: number;
  unknown: number;
  median: number | null;
  p75: number | null;
  p90: number | null;
}

function stats(values: number[], unknown: number): Stats {
  const sorted = [...values].sort((a, b) => a - b);
  return {
    n: sorted.length,
    unknown,
    median: percentile(sorted, 0.5),
    p75: percentile(sorted, 0.75),
    p90: percentile(sorted, 0.9),
  };
}

export interface LeadBucket extends Stats {
  start: number;
  end: number;
}

export interface LeadPoint {
  /** When the PR merged (ms) and the stage's duration (s). */
  ms: number;
  seconds: number;
  pr: PrMilestones;
}

export interface StageSeries {
  key: StageKey;
  label: string;
  /** Over every PR merged in the window. */
  summary: Stats;
  buckets: LeadBucket[];
  /** One per PR with the stage known. */
  points: LeadPoint[];
}

export interface LeadTimeSeries {
  buckets: Bucket[];
  stages: StageSeries[];
  /** PRs merged in the window. */
  merged: number;
}

/** Bucket length in days: 3 for the 4-week window (enough points), else 7. */
export function bucketDays(weeks: number): number {
  return weeks <= 4 ? 3 : 7;
}

/**
 * Every PR merged in `[windowStart, now]`, per stage and bucket. A bucket
 * with no PR whose stage is known has null statistics (a gap, never zero).
 */
export function leadTimeSeries(
  prs: readonly PrMilestones[],
  options: DurationOptions & { windowStart: number; now: number; days: number },
): LeadTimeSeries {
  const { windowStart, now, days, timeZone } = options;
  const buckets = localBuckets(windowStart, now, days, timeZone);
  const merged = prs.filter((pr) => {
    if (pr.state !== "MERGED" || pr.ended === null) return false;
    const at = Date.parse(pr.ended);
    return at >= windowStart && at <= now;
  });
  const stages = STAGES.map(({ key, label }): StageSeries => {
    const values: number[][] = buckets.map(() => []);
    const unknown: number[] = buckets.map(() => 0);
    const points: LeadPoint[] = [];
    let unknownAll = 0;
    for (const pr of merged) {
      const at = Date.parse(pr.ended as string);
      const index = bucketIndex(buckets, at);
      const seconds = stageSeconds(pr, key, options);
      if (seconds === null) {
        unknownAll++;
        if (index >= 0) unknown[index]++;
      } else {
        points.push({ ms: at, seconds, pr });
        if (index >= 0) values[index].push(seconds);
      }
    }
    return {
      key,
      label,
      summary: stats(
        points.map((p) => p.seconds),
        unknownAll,
      ),
      buckets: buckets.map((b, i) => ({ start: b.start, end: b.end, ...stats(values[i], unknown[i]) })),
      points,
    };
  });
  return { buckets, stages, merged: merged.length };
}
