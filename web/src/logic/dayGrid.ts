// Calendar decoration for a card's timeline: a line at each local midnight
// (labelled with the weekday that starts there) and a band over each weekend.
// Days are calendar days in a timezone, not multiples of 24 hours, so a DST
// day is 23 or 25 hours long. This is decoration, not PR state: nothing here
// goes through the state catalog.
import { DAY, MINUTE } from "./format";

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;

export interface DayLine {
  /** The local midnight (ms). */
  t: number;
  /** Fraction of the track. */
  x: number;
  /** 0 = Sunday ... 6 = Saturday. */
  weekday: number;
  /** "Mon" ... "Sun". */
  label: string;
}

/** Saturday 00:00 to Monday 00:00 local time, clipped to the track. */
export interface WeekendSpan {
  t0: number;
  t1: number;
  x0: number;
  x1: number;
}

export interface DayGrid {
  days: DayLine[];
  weekends: WeekendSpan[];
}

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
    });
    formatters.set(timeZone, found);
  }
  return found;
}

/** The wall-clock time in `timeZone` at instant `ms`, read as if it were UTC. */
function wallClock(ms: number, timeZone: string): number {
  const parts: Record<string, number> = {};
  for (const part of formatter(timeZone).formatToParts(ms)) {
    if (part.type !== "literal") parts[part.type] = Number(part.value);
  }
  return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
}

/** The timezone's offset from UTC (ms, east positive) at instant `ms`. */
function offsetAt(ms: number, timeZone: string): number {
  return wallClock(ms, timeZone) - Math.floor(ms / 1000) * 1000;
}

/** The instant of local midnight starting the calendar day numbered `day` (days since 1970-01-01). */
function midnight(day: number, timeZone: string): number {
  const guess = day * DAY;
  const first = guess - offsetAt(guess, timeZone);
  // Across a DST change the offset at the guess is not the offset at midnight.
  return guess - offsetAt(first, timeZone);
}

function calendarDay(ms: number, timeZone: string): number {
  return Math.floor(wallClock(ms, timeZone) / DAY);
}

/** 0 = Sunday ... 6 = Saturday; day 0 (1970-01-01) was a Thursday. */
const weekdayOf = (day: number): number => (((day + 4) % 7) + 7) % 7;

/** The viewer's own timezone. */
export const localTimeZone = (): string => Intl.DateTimeFormat().resolvedOptions().timeZone;

/**
 * Day lines (local midnights strictly inside the track) and weekend spans
 * (clipped to it) for a track from `start` to `end` (ms).
 */
export function dayGrid(start: number, end: number, timeZone: string = localTimeZone()): DayGrid {
  const span = Math.max(end - start, MINUTE);
  const xOf = (ms: number): number => (ms - start) / span;
  const firstDay = calendarDay(start, timeZone);
  const lastDay = calendarDay(end, timeZone);
  const days: DayLine[] = [];
  const weekends: WeekendSpan[] = [];
  let weekendEnd: number | null = null;
  let previous = midnight(firstDay, timeZone);
  for (let day = firstDay; day <= lastDay; day++) {
    // The next midnight ends this day, so a DST day is as long as it really was.
    const next = midnight(day + 1, timeZone);
    const weekday = weekdayOf(day);
    if (previous > start && previous < end) {
      days.push({ t: previous, x: xOf(previous), weekday, label: WEEKDAYS[weekday] });
    }
    if (weekday === 6 || weekday === 0) {
      const t0 = Math.max(previous, start);
      const t1 = Math.min(next, end);
      if (t1 > t0) {
        const last = weekends[weekends.length - 1];
        if (last && weekendEnd === previous) {
          last.t1 = t1;
          last.x1 = xOf(t1);
        } else {
          weekends.push({ t0, t1, x0: xOf(t0), x1: xOf(t1) });
        }
        weekendEnd = next;
      }
    }
    previous = next;
  }
  return { days, weekends };
}

/** Under this many px per day, only Mondays get a line. */
export const DAY_LINE_MIN_PX = 14;
/** Under this many px per day, only the weekend bands are drawn. */
export const WEEK_LINE_MIN_PX = 5;
/** At least this many px per day (or Mondays only), a label has room for three letters. */
export const DAY_LABEL_MIN_PX = 26;

export interface GridDensity {
  /** Which midnights get a line: every day, only Mondays, or none. */
  lines: "days" | "weeks" | "none";
  /** Letters a label gets: the whole "Mon", or its initial where daily labels would collide. */
  letters: 1 | 3;
}

/** How much of the grid fits a track `trackPx` wide covering `spanMs`. */
export function gridDensity(trackPx: number, spanMs: number): GridDensity {
  const pxPerDay = trackPx / (Math.max(spanMs, MINUTE) / DAY);
  return {
    lines: pxPerDay >= DAY_LINE_MIN_PX ? "days" : pxPerDay >= WEEK_LINE_MIN_PX ? "weeks" : "none",
    letters: pxPerDay >= DAY_LABEL_MIN_PX || pxPerDay < DAY_LINE_MIN_PX ? 3 : 1,
  };
}

/** The day lines to draw at that density. */
export function visibleDays(grid: DayGrid, density: GridDensity): DayLine[] {
  if (density.lines === "none") return [];
  return density.lines === "weeks" ? grid.days.filter((d) => d.weekday === 1) : grid.days;
}
