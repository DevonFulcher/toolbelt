import { describe, expect, test } from "vitest";
import {
  addLocalDays,
  bucketIndex,
  formatLocalDay,
  formatLocalInstant,
  isWeekend,
  localBuckets,
  localMidnight,
  localParts,
  startOfLocalDay,
  startOfLocalWeek,
  weekdaySeconds,
  windowStart,
} from "./localTime";

const CHICAGO = "America/Chicago";
const UTC = "UTC";
const HOUR = 3_600_000;
const iso = (text: string): number => Date.parse(text);

describe("local days", () => {
  test("a Friday 19:00 in Chicago is Saturday in UTC but not a weekend there", () => {
    const friday7pm = iso("2026-10-10T00:00:00Z"); // 19:00 CDT, Fri Oct 9

    expect(localParts(friday7pm, CHICAGO)).toMatchObject({ day: 9, hour: 19, weekday: 5 });
    expect(isWeekend(friday7pm, CHICAGO)).toBe(false);
    expect(isWeekend(friday7pm, UTC)).toBe(true);
    expect(formatLocalDay(friday7pm, CHICAGO)).toBe("Fri Oct 9");
    expect(formatLocalInstant(friday7pm, CHICAGO)).toBe("Fri Oct 9, 19:00");
  });

  test("local midnight is where the zone says, and the day is 24 hours in October", () => {
    const midnight = localMidnight(2026, 10, 9, CHICAGO);

    expect(midnight).toBe(iso("2026-10-09T05:00:00Z"));
    expect(startOfLocalDay(iso("2026-10-09T20:00:00Z"), CHICAGO)).toBe(midnight);
    expect(addLocalDays(midnight, 1, CHICAGO) - midnight).toBe(24 * HOUR);
  });

  test("a day is 23 hours when clocks go forward and 25 when they go back", () => {
    const spring = localMidnight(2026, 3, 8, CHICAGO);
    const fall = localMidnight(2026, 11, 1, CHICAGO);

    expect(addLocalDays(spring, 1, CHICAGO) - spring).toBe(23 * HOUR);
    expect(addLocalDays(fall, 1, CHICAGO) - fall).toBe(25 * HOUR);
    // Adding days is calendar arithmetic: a week across the change is still
    // seven local midnights.
    const week = addLocalDays(localMidnight(2026, 10, 26, CHICAGO), 7, CHICAGO);
    expect(week).toBe(localMidnight(2026, 11, 2, CHICAGO));
    expect(week - localMidnight(2026, 10, 26, CHICAGO)).toBe(7 * 24 * HOUR + HOUR);
  });

  test("weeks start on Monday, in the viewer's zone", () => {
    const sundayEvening = iso("2026-10-12T02:00:00Z"); // Sun Oct 11 21:00 CDT

    expect(startOfLocalWeek(sundayEvening, CHICAGO)).toBe(localMidnight(2026, 10, 5, CHICAGO));
    expect(startOfLocalWeek(sundayEvening, UTC)).toBe(iso("2026-10-12T00:00:00Z"));
  });

  test("a window is whole weeks ending with the current one", () => {
    const now = iso("2026-10-15T15:00:00Z"); // Thu Oct 15

    expect(windowStart(now, 4, CHICAGO)).toBe(localMidnight(2026, 9, 21, CHICAGO));
    expect(windowStart(now, 1, UTC)).toBe(iso("2026-10-12T00:00:00Z"));
  });
});

describe("buckets", () => {
  test("daily buckets cover the window to the end of the current day and flag weekends", () => {
    const from = localMidnight(2026, 10, 5, CHICAGO); // Monday
    const now = iso("2026-10-11T03:00:00Z"); // Sat Oct 10 22:00 CDT

    const buckets = localBuckets(from, now, 1, CHICAGO);

    expect(buckets).toHaveLength(6);
    expect(buckets.map((b) => b.weekend)).toEqual([false, false, false, false, false, true]);
    expect(buckets[5].end).toBe(localMidnight(2026, 10, 11, CHICAGO));
  });

  test("buckets across a DST change keep calendar days", () => {
    const from = localMidnight(2026, 10, 26, CHICAGO);

    const buckets = localBuckets(from, iso("2026-11-02T12:00:00Z"), 1, CHICAGO);

    expect(buckets.map((b) => (b.end - b.start) / HOUR)).toEqual([24, 24, 24, 24, 24, 24, 25, 24]);
    expect(buckets.map((b) => b.weekend)).toEqual([false, false, false, false, false, true, true, false]);
  });

  test("a time belongs to the bucket that contains it; outside them all is -1", () => {
    const from = localMidnight(2026, 10, 5, CHICAGO);
    const buckets = localBuckets(from, from + 3 * 24 * HOUR, 1, CHICAGO);

    expect(bucketIndex(buckets, from)).toBe(0);
    expect(bucketIndex(buckets, from + 24 * HOUR - 1)).toBe(0);
    expect(bucketIndex(buckets, from + 24 * HOUR)).toBe(1);
    expect(bucketIndex(buckets, from - 1)).toBe(-1);
    expect(bucketIndex(buckets, buckets[buckets.length - 1].end)).toBe(-1);
    expect(bucketIndex([], 5)).toBe(-1);
  });
});

describe("weekdaySeconds", () => {
  const at = (day: number, hour: number, zone = CHICAGO): number =>
    localMidnight(2026, 10, day, zone) + hour * HOUR;

  test("Friday 17:00 to Monday 09:00 is 16 hours of weekdays out of 64", () => {
    const from = at(9, 17);
    const to = at(12, 9);

    expect((to - from) / HOUR).toBe(64);
    expect(weekdaySeconds(from, to, CHICAGO) / 3600).toBe(16);
  });

  test("an interval inside a weekend is 0, and one starting or ending on it loses that part", () => {
    expect(weekdaySeconds(at(10, 3), at(11, 20), CHICAGO)).toBe(0);
    // Sat 12:00 to Mon 03:00: only Monday's 3 hours.
    expect(weekdaySeconds(at(10, 12), at(12, 3), CHICAGO) / 3600).toBe(3);
    // Thu 20:00 to Sat 12:00: Thursday 4h + Friday 24h.
    expect(weekdaySeconds(at(8, 20), at(10, 12), CHICAGO) / 3600).toBe(28);
  });

  test("weekdays only is unchanged, a start equal to the end is 0, an end before the start is 0", () => {
    expect(weekdaySeconds(at(6, 9), at(7, 9), CHICAGO)).toBe(24 * 3600);
    expect(weekdaySeconds(at(6, 9), at(6, 9), CHICAGO)).toBe(0);
    expect(weekdaySeconds(at(7, 9), at(6, 9), CHICAGO)).toBe(0);
  });

  test("a span of several weeks loses two days per weekend", () => {
    const from = localMidnight(2026, 10, 5, CHICAGO); // a Monday
    const to = localMidnight(2026, 10, 26, CHICAGO); // three weeks later

    expect(weekdaySeconds(from, to, CHICAGO) / 86400).toBe(15);
  });

  test("a week with a DST change counts its 25-hour Sunday as weekend and weekdays as they are", () => {
    const from = localMidnight(2026, 10, 26, CHICAGO);
    const to = localMidnight(2026, 11, 2, CHICAGO);

    expect((to - from) / HOUR).toBe(7 * 24 + 1);
    expect(weekdaySeconds(from, to, CHICAGO) / 3600).toBe(5 * 24);
  });

  test("the zone decides what is a weekend: the same instants differ in UTC", () => {
    const from = iso("2026-10-10T00:00:00Z"); // Fri 19:00 CDT, Sat 00:00 UTC
    const to = iso("2026-10-10T12:00:00Z"); // Sat 07:00 CDT

    expect(weekdaySeconds(from, to, UTC)).toBe(0);
    expect(weekdaySeconds(from, to, CHICAGO) / 3600).toBe(5);
  });
});
