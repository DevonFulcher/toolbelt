// What a day, a week and a weekend are, in the viewer's time zone.
//
// The server speaks UTC instants only; every calendar notion lives here, so
// there is one source of truth (the browser's `Intl`). Every function takes
// the IANA zone explicitly (the callers default it to the browser's), which
// is how the tests run in fixed zones. Days are calendar days: a day is
// 23 or 25 hours long across a DST change, and weeks start on Monday.

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(timeZone: string): Intl.DateTimeFormat {
  let found = formatters.get(timeZone);
  if (!found) {
    found = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      second: "numeric",
      weekday: "short",
    });
    formatters.set(timeZone, found);
  }
  return found;
}

/** The viewer's IANA zone (what `Intl` resolves for the browser). */
export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

export interface LocalParts {
  year: number;
  /** 1-12 */
  month: number;
  day: number;
  /** 1 (Monday) to 7 (Sunday) */
  weekday: number;
  hour: number;
  minute: number;
  second: number;
}

const WEEKDAY_NUMBER: Record<string, number> = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 };
export const WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** The wall-clock fields of `ms` in `timeZone`. */
export function localParts(ms: number, timeZone: string): LocalParts {
  const fields: Record<string, string> = {};
  for (const part of formatter(timeZone).formatToParts(new Date(ms))) fields[part.type] = part.value;
  return {
    year: Number(fields.year),
    month: Number(fields.month),
    day: Number(fields.day),
    weekday: WEEKDAY_NUMBER[fields.weekday],
    hour: Number(fields.hour) % 24,
    minute: Number(fields.minute),
    second: Number(fields.second),
  };
}

/** The zone's offset from UTC at `ms`, in ms (local wall clock minus UTC). */
function offsetAt(ms: number, timeZone: string): number {
  const p = localParts(ms, timeZone);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(ms / 1000) * 1000;
}

/**
 * The instant of 00:00 on the given calendar date in `timeZone` (a day number
 * beyond the month's length rolls over, so `day + 7` is a week later). In a
 * zone whose midnight does not exist that day, the first instant after it.
 */
export function localMidnight(year: number, month: number, day: number, timeZone: string): number {
  const wall = Date.UTC(year, month - 1, day);
  const first = wall - offsetAt(wall, timeZone);
  return wall - offsetAt(first, timeZone);
}

/** 00:00 of the local day `ms` falls on. */
export function startOfLocalDay(ms: number, timeZone: string): number {
  const p = localParts(ms, timeZone);
  return localMidnight(p.year, p.month, p.day, timeZone);
}

/** The midnight `days` calendar days after the local midnight `dayStart`. */
export function addLocalDays(dayStart: number, days: number, timeZone: string): number {
  const p = localParts(dayStart, timeZone);
  return localMidnight(p.year, p.month, p.day + days, timeZone);
}

/** Monday 00:00 of the local week `ms` falls in. */
export function startOfLocalWeek(ms: number, timeZone: string): number {
  const day = startOfLocalDay(ms, timeZone);
  return addLocalDays(day, -(localParts(day, timeZone).weekday - 1), timeZone);
}

/** Whether `ms` falls on a Saturday or Sunday in `timeZone`. */
export function isWeekend(ms: number, timeZone: string): boolean {
  return localParts(ms, timeZone).weekday >= 6;
}

/**
 * The first instant of a window of `weeks` calendar weeks ending with the week
 * `now` is in: the Monday `weeks - 1` weeks before that week's.
 */
export function windowStart(now: number, weeks: number, timeZone: string): number {
  return addLocalDays(startOfLocalWeek(now, timeZone), -(weeks - 1) * 7, timeZone);
}

export interface Bucket {
  /** First instant (inclusive) and the next bucket's start (exclusive). */
  start: number;
  end: number;
  /** A one-day bucket that is a Saturday or Sunday. */
  weekend: boolean;
}

/**
 * Consecutive buckets of `days` local calendar days from `from` (a local
 * midnight) until `until`: the last one runs to its natural end, past `until`
 * when `until` is not on a boundary (it is the current, partial, bucket).
 */
export function localBuckets(from: number, until: number, days: number, timeZone: string): Bucket[] {
  const buckets: Bucket[] = [];
  let start = from;
  while (start <= until) {
    const end = addLocalDays(start, days, timeZone);
    buckets.push({ start, end, weekend: days === 1 && isWeekend(start, timeZone) });
    start = end;
  }
  return buckets;
}

/** The index of the bucket `ms` falls in, or -1 outside them all. */
export function bucketIndex(buckets: readonly Bucket[], ms: number): number {
  if (buckets.length === 0 || ms < buckets[0].start || ms >= buckets[buckets.length - 1].end) return -1;
  let lo = 0;
  let hi = buckets.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (buckets[mid].start <= ms) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

/**
 * The seconds between `from` and `to` that fall on Monday to Friday in
 * `timeZone`: elapsed time minus whatever lies on a Saturday or Sunday (an
 * interval inside a weekend is 0). Walks calendar days, so a 23- or 25-hour
 * day counts for what it is. No holiday calendar. `to < from` is 0.
 */
export function weekdaySeconds(from: number, to: number, timeZone: string): number {
  if (to <= from) return 0;
  let total = 0;
  let day = startOfLocalDay(from, timeZone);
  while (day < to) {
    const next = addLocalDays(day, 1, timeZone);
    if (localParts(Math.max(day, from), timeZone).weekday < 6) {
      total += Math.min(next, to) - Math.max(day, from);
    }
    day = next;
  }
  return total / 1000;
}

// ---- labels ----------------------------------------------------------------------

/** "Oct 5" for `ms` in `timeZone`. */
export function formatLocalDate(ms: number, timeZone: string): string {
  const p = localParts(ms, timeZone);
  return `${MONTH_SHORT[p.month - 1]} ${p.day}`;
}

/** "Sat Oct 10" */
export function formatLocalDay(ms: number, timeZone: string): string {
  return `${WEEKDAY_SHORT[localParts(ms, timeZone).weekday - 1]} ${formatLocalDate(ms, timeZone)}`;
}

/** "Sat Oct 10, 19:00" (24-hour, in `timeZone`). */
export function formatLocalInstant(ms: number, timeZone: string): string {
  const p = localParts(ms, timeZone);
  const hh = String(p.hour).padStart(2, "0");
  const mm = String(p.minute).padStart(2, "0");
  return `${formatLocalDay(ms, timeZone)}, ${hh}:${mm}`;
}
