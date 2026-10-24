// The aggregate view's agent activity: the server's per-hour sums of agent
// turns made into the viewer's local days (4 and 12 weeks) or weeks (26, 52),
// and totals over the window. Pure; the zone is injected.
import type { AggregateActivity } from "../api/generated";
import { bucketIndex, localBuckets, type Bucket } from "./localTime";
import { granularityFor } from "./throughput";

export interface ActivityTotals {
  turns: number;
  calls: number;
  wallMs: number;
  modelMs: number;
  toolMs: number;
  delegatedMs: number;
  blockedMs: number;
}

export interface ActivityBuckets {
  buckets: Bucket[];
  /** Time per bucket, ms, in the parts a turn's wall time splits into. */
  model: number[];
  tool: number[];
  delegated: number[];
  blocked: number[];
  /** Summed call time per bucket for each category, biggest total first. */
  categories: { name: string; values: number[]; total: number }[];
  totals: ActivityTotals;
  /** Tools by summed call time over the whole window. */
  tools: { name: string; calls: number; ms: number }[];
}

export function activityBuckets(
  activity: AggregateActivity,
  options: { windowStart: number; now: number; weeks: number; timeZone: string },
): ActivityBuckets {
  const buckets = localBuckets(
    options.windowStart,
    options.now,
    granularityFor(options.weeks) === "day" ? 1 : 7,
    options.timeZone,
  );
  const zeros = (): number[] => buckets.map(() => 0);
  const model = zeros();
  const tool = zeros();
  const delegated = zeros();
  const blocked = zeros();
  const categories = new Map<string, number[]>();
  const tools = new Map<string, { calls: number; ms: number }>();
  const totals: ActivityTotals = { turns: 0, calls: 0, wallMs: 0, modelMs: 0, toolMs: 0, delegatedMs: 0, blockedMs: 0 };
  for (const hour of activity.hours) {
    const index = bucketIndex(buckets, Date.parse(hour.t));
    if (index < 0) continue;
    model[index] += hour.model_ms;
    tool[index] += hour.tool_ms;
    delegated[index] += hour.delegated_ms;
    blocked[index] += hour.blocked_ms;
    totals.turns += hour.turns;
    totals.calls += hour.calls;
    totals.wallMs += hour.wall_ms;
    totals.modelMs += hour.model_ms;
    totals.toolMs += hour.tool_ms;
    totals.delegatedMs += hour.delegated_ms;
    totals.blockedMs += hour.blocked_ms;
    for (const category of hour.categories) {
      const row = categories.get(category.name) ?? zeros();
      row[index] += category.ms;
      categories.set(category.name, row);
    }
    for (const entry of hour.tools) {
      const row = tools.get(entry.name) ?? { calls: 0, ms: 0 };
      row.calls += entry.calls;
      row.ms += entry.ms;
      tools.set(entry.name, row);
    }
  }
  return {
    buckets,
    model,
    tool,
    delegated,
    blocked,
    categories: [...categories.entries()]
      .map(([name, values]) => ({ name, values, total: values.reduce((sum, v) => sum + v, 0) }))
      .sort((a, b) => b.total - a.total),
    totals,
    tools: [...tools.entries()].map(([name, row]) => ({ name, ...row })).sort((a, b) => b.ms - a.ms),
  };
}

/** A duration as "2h 05m", "4m 10s" or "12s". */
export function formatDuration(ms: number): string {
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}
