import { describe, expect, test } from "vitest";
import type { PrMilestones } from "../api/generated";
import { localMidnight, windowStart } from "./localTime";
import { granularityFor, linesThroughput, throughput } from "./throughput";

const CHICAGO = "America/Chicago";

function pr(number: number, opened: string, ended: string | null, state: PrMilestones["state"]): PrMilestones {
  return {
    repo: "o/api",
    number,
    title: "t",
    url: "",
    state,
    opened,
    ended,
    task_started: null,
    matched_by: null,
    first_review_requested: null,
    first_review: null,
    first_review_after_request: null,
    additions: 10,
    deletions: 2,
  };
}

const NOW = Date.parse("2026-10-15T15:00:00Z"); // Thursday

describe("throughput", () => {
  test("is daily for 4 and 12 weeks and weekly beyond", () => {
    expect([4, 12, 26, 52].map(granularityFor)).toEqual(["day", "day", "week", "week"]);
    const options = (weeks: number) => ({
      windowStart: windowStart(NOW, weeks, CHICAGO),
      now: NOW,
      weeks,
      timeZone: CHICAGO,
    });

    expect(throughput([], options(4)).buckets).toHaveLength(3 * 7 + 4);
    expect(throughput([], options(12)).buckets).toHaveLength(11 * 7 + 4);
    expect(throughput([], options(26)).buckets).toHaveLength(26);
    expect(throughput([], options(52)).buckets).toHaveLength(52);
  });

  test("counts by the viewer's local day: a Friday 19:00 merge is Friday, not Saturday", () => {
    const prs = [
      pr(1, "2026-10-08T15:00:00Z", "2026-10-10T00:00:00Z", "MERGED"), // Fri 19:00 CDT
      pr(2, "2026-10-10T00:30:00Z", null, "OPEN"),
      pr(3, "2026-10-12T15:00:00Z", "2026-10-13T15:00:00Z", "CLOSED"),
    ];

    const result = throughput(prs, {
      windowStart: windowStart(NOW, 4, CHICAGO),
      now: NOW,
      weeks: 4,
      timeZone: CHICAGO,
    });
    const day = (m: number, d: number) =>
      result.buckets.findIndex((b) => b.start === localMidnight(2026, m, d, CHICAGO));

    expect(result.merged[day(10, 9)]).toBe(1);
    expect(result.merged[day(10, 10)]).toBe(0);
    expect(result.opened[day(10, 9)]).toBe(1); // Fri 19:30 CDT
    expect(result.opened[day(10, 8)]).toBe(1);
    expect(result.closed[day(10, 13)]).toBe(1);
    expect(result.buckets[day(10, 10)].weekend).toBe(true);
    expect(result.buckets[day(10, 9)].weekend).toBe(false);
  });

  test("PRs outside the window are not counted, and weekly buckets start on Monday", () => {
    const prs = [
      pr(1, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "MERGED"),
      pr(2, "2026-10-12T05:00:00Z", "2026-10-12T06:00:00Z", "MERGED"), // Sun 23:00-Mon 01:00 CDT
    ];

    const result = throughput(prs, {
      windowStart: windowStart(NOW, 26, CHICAGO),
      now: NOW,
      weeks: 26,
      timeZone: CHICAGO,
    });

    expect(result.opened.reduce((a, b) => a + b, 0)).toBe(1);
    expect(result.merged[result.merged.length - 1]).toBe(1);
    expect(result.buckets[result.buckets.length - 1].start).toBe(localMidnight(2026, 10, 12, CHICAGO));
  });

  test("excluding weekends removes weekend events in daily and weekly windows", () => {
    const prs = [
      pr(1, "2026-10-09T18:00:00Z", "2026-10-10T18:00:00Z", "MERGED"),
      pr(2, "2026-10-10T18:00:00Z", "2026-10-12T18:00:00Z", "MERGED"),
    ];
    for (const weeks of [4, 26]) {
      const result = throughput(prs, {
        windowStart: windowStart(NOW, weeks, CHICAGO), now: NOW, weeks, timeZone: CHICAGO, excludeWeekends: true,
      });
      expect(result.opened.reduce((sum, n) => sum + n, 0)).toBe(1);
      expect(result.merged.reduce((sum, n) => sum + n, 0)).toBe(1);
    }
  });
});

describe("linesThroughput", () => {
  const options = {
    windowStart: Date.parse("2026-10-05T05:00:00Z"),
    now: NOW,
    weeks: 4,
    timeZone: CHICAGO,
  };
  const merged = (number: number, ended: string, additions: number, deletions: number): PrMilestones => ({
    ...pr(number, "2026-10-01T15:00:00Z", ended, "MERGED"),
    additions,
    deletions,
  });

  test("sums the lines of the PRs merged in each day", () => {
    const result = linesThroughput(
      [
        merged(1, "2026-10-07T15:00:00Z", 100, 10),
        merged(2, "2026-10-07T20:00:00Z", 50, 5),
        merged(3, "2026-10-08T15:00:00Z", 7, 3),
      ],
      options,
    );

    const day = (iso: string): number => {
      const d = new Date(iso);
      return result.buckets.findIndex((b) => b.start === localMidnight(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate(), CHICAGO));
    };
    expect(result.granularity).toBe("day");
    expect(result.added[day("2026-10-07T15:00:00Z")]).toBe(150);
    expect(result.deleted[day("2026-10-07T15:00:00Z")]).toBe(15);
    expect(result.added[day("2026-10-08T15:00:00Z")]).toBe(7);
    expect(result.added.reduce((a, b) => a + b, 0)).toBe(157);
  });

  test("counts only merged PRs, by when they merged", () => {
    const closed = { ...pr(4, "2026-10-01T15:00:00Z", "2026-10-07T15:00:00Z", "CLOSED"), additions: 999, deletions: 999 };
    const open = { ...pr(5, "2026-10-01T15:00:00Z", null, "OPEN"), additions: 888, deletions: 888 };
    const before = merged(6, "2026-09-01T15:00:00Z", 777, 777);

    const result = linesThroughput([closed, open, before], options);

    expect(result.added.every((n) => n === 0)).toBe(true);
    expect(result.deleted.every((n) => n === 0)).toBe(true);
  });

  test("leaves out merges on weekends when asked", () => {
    const saturday = merged(7, "2026-10-10T17:00:00Z", 40, 4);
    const monday = merged(8, "2026-10-12T17:00:00Z", 60, 6);

    const all = linesThroughput([saturday, monday], options);
    const weekdays = linesThroughput([saturday, monday], { ...options, excludeWeekends: true });

    expect(all.added.reduce((a, b) => a + b, 0)).toBe(100);
    expect(weekdays.added.reduce((a, b) => a + b, 0)).toBe(60);
    expect(weekdays.deleted.reduce((a, b) => a + b, 0)).toBe(6);
  });

  test("buckets by week for the long windows", () => {
    const result = linesThroughput([merged(9, "2026-10-07T15:00:00Z", 5, 1)], { ...options, weeks: 26, windowStart: Date.parse("2026-04-20T05:00:00Z") });

    expect(result.granularity).toBe("week");
    expect(result.added.reduce((a, b) => a + b, 0)).toBe(5);
  });
});
