import { formatLocalDate, localBuckets, localParts, startOfLocalDay, WEEKDAY_SHORT } from "./localTime";

// The arithmetic behind the aggregate view's charts: scales, ticks, stacking,
// paths and a linear duration axis. Pure functions; the
// components only draw what these return.
//
// (Medians and percentiles are in leadTime.ts; days, weeks and weekends in
// localTime.ts.)

/** Maps `domain` onto `range` linearly. */
export function linearScale(
  [d0, d1]: readonly [number, number],
  [r0, r1]: readonly [number, number],
): (value: number) => number {
  const span = d1 - d0;
  return (value) => (span === 0 ? r0 : r0 + ((value - d0) / span) * (r1 - r0));
}

/** A rounded-up axis maximum for counts: 1, 2, 5, 10, 20, 50... (at least 1). */
export function niceMax(max: number): number {
  if (max <= 1) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(max));
  for (const step of [1, 2, 5, 10]) {
    if (max <= step * magnitude) return step * magnitude;
  }
  return 10 * magnitude;
}

/** Whole-number ticks from 0 to `max`, at most about `count` of them. */
export function countTicks(max: number, count = 5): number[] {
  const top = niceMax(max);
  let step = 1;
  while (top / step > count) step *= top / step > count * 2 ? 5 : 2;
  const ticks: number[] = [];
  for (let value = 0; value <= top; value += step) ticks.push(value);
  return ticks;
}

/** Indexes of about `want` evenly spread labels out of `count` buckets,
 * always including the first and, when it fits, the last. */
export function labelIndexes(count: number, want: number): number[] {
  if (count <= 0) return [];
  if (count <= want) return Array.from({ length: count }, (_, i) => i);
  const step = Math.ceil((count - 1) / (want - 1));
  const indexes: number[] = [];
  for (let i = 0; i < count; i += step) indexes.push(i);
  return indexes;
}

// ---- stacking ---------------------------------------------------------------------

export interface Band {
  /** Per bucket: [bottom, top] in 0..1, or null where the series has no data. */
  spans: ([number, number] | null)[];
}

/**
 * Stacks `shares` (series in bottom-to-top order, each a share per bucket or
 * null when nothing was in flight) into bands. A bucket with no data is null
 * in every band, so the chart shows a gap rather than inventing a split.
 */
export function stackShares(shares: (number | null)[][]): Band[] {
  const buckets = shares[0]?.length ?? 0;
  const bands: Band[] = shares.map(() => ({ spans: new Array(buckets).fill(null) }));
  for (let b = 0; b < buckets; b++) {
    if (shares.every((series) => series[b] === null)) continue;
    let bottom = 0;
    shares.forEach((series, s) => {
      const top = bottom + (series[b] ?? 0);
      bands[s].spans[b] = [bottom, top];
      bottom = top;
    });
  }
  return bands;
}

/**
 * The SVG path of one band: a closed area per run of consecutive buckets
 * with data. `x(i)` is a bucket's centre; a single-bucket run is drawn as a
 * column of width `columnWidth` so it is not invisible.
 */
export function bandPath(
  band: Band,
  x: (index: number) => number,
  y: (share: number) => number,
  columnWidth: number,
): string {
  const parts: string[] = [];
  let run: number[] = [];
  const flush = (): void => {
    if (run.length === 0) return;
    const points: [number, number][] =
      run.length === 1
        ? [
            [x(run[0]) - columnWidth / 2, run[0]],
            [x(run[0]) + columnWidth / 2, run[0]],
          ]
        : run.map((i) => [x(i), i]);
    const top = points.map(([px, i]) => `${px},${y(band.spans[i]![1])}`);
    const bottom = [...points].reverse().map(([px, i]) => `${px},${y(band.spans[i]![0])}`);
    parts.push(`M${[...top, ...bottom].join("L")}Z`);
    run = [];
  };
  band.spans.forEach((span, i) => {
    if (span === null) flush();
    else run.push(i);
  });
  flush();
  return parts.join("");
}

// ---- lines over sampled instants -----------------------------------------------

/** The index of the sample nearest to `ms` in the ascending `times`. */
export function nearestIndex(times: readonly number[], ms: number): number {
  if (times.length === 0) return -1;
  let lo = 0;
  let hi = times.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < ms) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && ms - times[lo - 1] <= times[lo] - ms) return lo - 1;
  return lo;
}

/**
 * The SVG path of a line through `values` (one per sample, `null` for a gap):
 * one subpath per run of consecutive samples with data, so the line breaks at
 * a gap rather than being drawn across it. A run of a single sample is a
 * zero-length segment (drawn as a dot by a round line cap).
 */
export function linePath(
  values: readonly (number | null)[],
  x: (index: number) => number,
  y: (value: number) => number,
): string {
  const parts: string[] = [];
  let run: string[] = [];
  const flush = (): void => {
    if (run.length === 1) run.push(run[0].replace("M", "L"));
    if (run.length) parts.push(run.join(""));
    run = [];
  };
  values.forEach((value, i) => {
    if (value === null) flush();
    else run.push(`${run.length ? "L" : "M"}${x(i)},${y(value)}`);
  });
  flush();
  return parts.join("");
}

/** "Oct 5, 06:00" for an ISO time, in UTC (the samples are UTC instants). */
export function formatInstant(iso: string): string {
  const date = new Date(iso);
  const hh = String(date.getUTCHours()).padStart(2, "0");
  const mm = String(date.getUTCMinutes()).padStart(2, "0");
  return `${formatBucketDate(iso)}, ${hh}:${mm}`;
}

/** The median of the positive entries (0 when there are none). */
export function medianPositive(values: readonly number[]): number {
  const sorted = values.filter((v) => v > 0).sort((a, b) => a - b);
  if (sorted.length === 0) return 0;
  const middle = sorted.length >> 1;
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

// ---- durations --------------------------------------------------------------------

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;
const WEEK = 7 * DAY;

/** "40m", "5.5h", "2.3d", "3.1w": one decimal where it helps, no zeros. */
export function formatDuration(seconds: number): string {
  const trim = (value: number): string => String(Math.round(value * 10) / 10);
  if (seconds < MINUTE) return "<1m";
  if (seconds < HOUR) return `${Math.round(seconds / MINUTE)}m`;
  if (seconds < DAY) return `${trim(seconds / HOUR)}h`;
  if (seconds < 2 * WEEK) return `${trim(seconds / DAY)}d`;
  return `${trim(seconds / WEEK)}w`;
}

export interface DurationAxis {
  unit: "m" | "h" | "d";
  /** The top of the axis (seconds) and its ticks, from 0. */
  top: number;
  ticks: { value: number; label: string }[];
}

/**
 * A linear duration axis from 0 for values up to `max` seconds, in the unit
 * that reads best for the range: minutes below 2 hours, hours below 3 days,
 * days beyond. Ticks are whole numbers of that unit.
 */
export function durationAxis(max: number): DurationAxis {
  const unit = max < 2 * HOUR ? "m" : max < 3 * DAY ? "h" : "d";
  const size = unit === "m" ? MINUTE : unit === "h" ? HOUR : DAY;
  const ticks = countTicks(max / size);
  return {
    unit,
    top: ticks[ticks.length - 1] * size,
    ticks: ticks.map((value) => ({ value: value * size, label: `${value}${unit}` })),
  };
}

/**
 * The area between `lows` and `highs` (per bucket; both must be non-null for a
 * bucket to be drawn), one closed shape per run of consecutive buckets, so it
 * breaks at a gap. A single bucket is drawn `width` wide.
 */
export function rangePath(
  lows: readonly (number | null)[],
  highs: readonly (number | null)[],
  x: (index: number) => number,
  y: (value: number) => number,
  width: number,
): string {
  const parts: string[] = [];
  let run: number[] = [];
  const flush = (): void => {
    if (run.length === 0) return;
    const points: [number, number][] =
      run.length === 1
        ? [
            [x(run[0]) - width / 2, run[0]],
            [x(run[0]) + width / 2, run[0]],
          ]
        : run.map((i) => [x(i), i]);
    const top = points.map(([px, i]) => `${px},${y(highs[i] as number)}`);
    const bottom = [...points].reverse().map(([px, i]) => `${px},${y(lows[i] as number)}`);
    parts.push(`M${[...top, ...bottom].join("L")}Z`);
    run = [];
  };
  lows.forEach((low, i) => {
    if (low === null || highs[i] === null) flush();
    else run.push(i);
  });
  flush();
  return parts.join("");
}

/**
 * The label under each daily bar: Mondays carry the date ("Oct 5"), the other
 * days their short name ("Tue") when the bar is wide enough, else an initial
 * ("T"), and when bars are very narrow only the Mondays are labelled. `null`
 * is no label. `slotPx` is the bar's width.
 */
export function dayLabels(
  buckets: readonly { start: number }[],
  timeZone: string,
  slotPx: number,
): (string | null)[] {
  return buckets.map(({ start }) => {
    const { weekday } = localParts(start, timeZone);
    if (weekday === 1) return formatLocalDate(start, timeZone);
    if (slotPx >= 40) return WEEKDAY_SHORT[weekday - 1];
    if (slotPx >= 14) return WEEKDAY_SHORT[weekday - 1][0];
    return null;
  });
}

/**
 * The Saturday-and-Sunday spans between `from` and `to`, as `[start, end]`
 * (clipped to them), merging the two days of a weekend. The one source of the
 * weekend bands behind every time-axis chart.
 */
export function weekendSpans(from: number, to: number, timeZone: string): [number, number][] {
  const spans: [number, number][] = [];
  for (const day of localBuckets(startOfLocalDay(from, timeZone), to, 1, timeZone)) {
    if (!day.weekend) continue;
    const start = Math.max(day.start, from);
    const end = Math.min(day.end, to);
    if (end <= start) continue;
    const previous = spans[spans.length - 1];
    if (previous && previous[1] === day.start) previous[1] = end;
    else spans.push([start, end]);
  }
  return spans;
}

// ---- dates ----------------------------------------------------------------------------

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Oct 5" for an ISO time, in UTC (the server buckets by UTC day/week). */
export function formatBucketDate(iso: string): string {
  const date = new Date(iso);
  return `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}`;
}

/** "Oct 5 - Oct 11" for a bucket starting at `iso` and `days` long. */
export function formatBucketRange(iso: string, days: number): string {
  if (days <= 1) return formatBucketDate(iso);
  const end = new Date(Date.parse(iso) + (days - 1) * 86_400_000).toISOString();
  return `${formatBucketDate(iso)} - ${formatBucketDate(end)}`;
}
