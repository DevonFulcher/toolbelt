// The arithmetic behind the aggregate view's charts: scales, ticks, stacking,
// paths, a log duration axis and strip packing. Pure functions; the
// components only draw what these return.
//
// (Medians and percentiles are computed on the server, which sends them with
// the raw durations; see aggregate.py.)

/** Maps `domain` onto `range` linearly. */
export function linearScale(
  [d0, d1]: readonly [number, number],
  [r0, r1]: readonly [number, number],
): (value: number) => number {
  const span = d1 - d0;
  return (value) => (span === 0 ? r0 : r0 + ((value - d0) / span) * (r1 - r0));
}

/** Maps `domain` onto `range` on a log10 scale (domain values must be > 0). */
export function logScale(
  [d0, d1]: readonly [number, number],
  range: readonly [number, number],
): (value: number) => number {
  const scale = linearScale([Math.log10(d0), Math.log10(d1)], range);
  return (value) => scale(Math.log10(Math.max(value, d0)));
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

/** The ladder of axis ticks for a duration axis, in seconds. */
export const DURATION_LADDER = [
  5 * MINUTE,
  15 * MINUTE,
  HOUR,
  4 * HOUR,
  12 * HOUR,
  DAY,
  3 * DAY,
  WEEK,
  2 * WEEK,
  4 * WEEK,
  8 * WEEK,
  26 * WEEK,
];

/** "40m", "5.5h", "2.3d", "3.1w": one decimal where it helps, no zeros. */
export function formatDuration(seconds: number): string {
  const trim = (value: number): string => String(Math.round(value * 10) / 10);
  if (seconds < MINUTE) return "<1m";
  if (seconds < HOUR) return `${Math.round(seconds / MINUTE)}m`;
  if (seconds < DAY) return `${trim(seconds / HOUR)}h`;
  if (seconds < 2 * WEEK) return `${trim(seconds / DAY)}d`;
  return `${trim(seconds / WEEK)}w`;
}

/**
 * The domain of a log duration axis for `values` (seconds): the ladder rungs
 * just below the smallest and just above the largest value, at least two
 * rungs wide, and the ticks in between.
 */
export function durationAxis(values: number[]): { domain: [number, number]; ticks: number[] } {
  const positive = values.filter((v) => v > 0);
  const low = positive.length ? Math.min(...positive) : DURATION_LADDER[1];
  const high = positive.length ? Math.max(...positive) : DURATION_LADDER[5];
  let lo = 0;
  for (let i = 0; i < DURATION_LADDER.length; i++) if (DURATION_LADDER[i] <= low) lo = i;
  let hi = DURATION_LADDER.length - 1;
  for (let i = DURATION_LADDER.length - 1; i >= 0; i--) if (DURATION_LADDER[i] >= high) hi = i;
  if (hi === lo) hi = Math.min(lo + 1, DURATION_LADDER.length - 1);
  return {
    domain: [DURATION_LADDER[lo], DURATION_LADDER[hi]],
    ticks: DURATION_LADDER.slice(lo, hi + 1),
  };
}

// ---- strips -------------------------------------------------------------------------

/**
 * Packs points at pixel positions `xs` into rows so that dots of `diameter`
 * do not overlap: each goes in the first row whose last dot is far enough
 * left. Returns the row of each point (0 is the middle row; rows alternate
 * above and below it) and the number of rows used, capped at `maxRows`
 * (points beyond overlap in the last row).
 */
export function packStrip(
  xs: number[],
  diameter: number,
  maxRows: number,
): { rows: number[]; used: number } {
  const order = xs.map((x, i) => [x, i] as const).sort((a, b) => a[0] - b[0]);
  const lastX: number[] = [];
  const rows = new Array<number>(xs.length).fill(0);
  let used = 0;
  for (const [x, index] of order) {
    let row = lastX.findIndex((last) => x - last >= diameter);
    if (row === -1) row = Math.min(lastX.length, maxRows - 1);
    lastX[row] = x;
    rows[index] = row;
    used = Math.max(used, row + 1);
  }
  return { rows, used };
}

/** The y offset of packed row `row` (0 centred, then +1, -1, +2, ...). */
export function rowOffset(row: number, diameter: number): number {
  const step = Math.ceil(row / 2) * diameter;
  return row % 2 === 1 ? -step : step;
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
