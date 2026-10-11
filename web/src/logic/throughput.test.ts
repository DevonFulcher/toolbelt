import { describe, expect, test } from "vitest";
import type { PrMilestones } from "../api/generated";
import { localMidnight, windowStart } from "./localTime";
import { granularityFor, throughput } from "./throughput";

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
