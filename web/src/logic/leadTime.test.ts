import { describe, expect, test } from "vitest";
import type { PrMilestones } from "../api/generated";
import { bucketDays, leadTimeSeries, percentile, stageSeconds, STAGES } from "./leadTime";
import { localMidnight } from "./localTime";

const CHICAGO = "America/Chicago";
const HOUR = 3600;

function pr(number: number, parts: Partial<PrMilestones>): PrMilestones {
  return {
    repo: "o/api",
    number,
    title: `PR ${number}`,
    url: `https://github.com/o/api/pull/${number}`,
    state: "MERGED",
    opened: "2026-10-06T15:00:00Z",
    ended: "2026-10-08T15:00:00Z",
    task_started: null,
    matched_by: null,
    first_review_requested: null,
    first_review: null,
    first_review_after_request: null,
    additions: 10,
    deletions: 2,
    ...parts,
  };
}

const WINDOW_START = localMidnight(2026, 9, 21, CHICAGO);
const NOW = Date.parse("2026-10-15T15:00:00Z");
const options = { windowStart: WINDOW_START, now: NOW, days: 7, timeZone: CHICAGO, excludeWeekends: false };

describe("percentile", () => {
  test("interpolates linearly between the closest ranks", () => {
    expect(percentile([], 0.5)).toBeNull();
    expect(percentile([7], 0.9)).toBe(7);
    expect(percentile([1, 2, 3, 4], 0.5)).toBe(2.5);
    expect(percentile([0, 10], 0.75)).toBe(7.5);
  });
});

describe("stage durations", () => {
  const full = pr(1, {
    task_started: "2026-10-06T10:00:00Z",
    first_review_requested: "2026-10-06T17:00:00Z",
    first_review: "2026-10-07T01:00:00Z",
    first_review_after_request: "2026-10-07T01:00:00Z",
  });

  test("each stage is measured between its two milestones, and unknown stays unknown", () => {
    const seconds = (key: (typeof STAGES)[number]["key"], p = full) =>
      stageSeconds(p, key, { excludeWeekends: false, timeZone: CHICAGO });

    expect(seconds("start_to_open")).toBe(5 * HOUR);
    expect(seconds("open_to_request")).toBe(2 * HOUR);
    expect(seconds("request_to_review")).toBe(8 * HOUR);
    expect(seconds("review_to_merge")).toBe(38 * HOUR);
    expect(seconds("start_to_merge")).toBe(53 * HOUR);
    const bare = pr(2, {});
    expect(seconds("start_to_open", bare)).toBeNull();
    expect(seconds("open_to_request", bare)).toBeNull();
    expect(seconds("review_to_merge", bare)).toBeNull();
    expect(seconds("start_to_merge", bare)).toBeNull();
  });
});

describe("lead time over time", () => {
  test("buckets by the local day of the merge: Friday 19:00 Central is Friday's week, not next", () => {
    // Fri Oct 9 19:00 CDT = Sat Oct 10 00:00 UTC; a UTC week would still be
    // this one, so use the Sunday boundary: Sun Oct 11 23:00 CDT = Mon 04:00Z.
    const sundayNight = pr(1, {
      ended: "2026-10-12T04:00:00Z",
      task_started: "2026-10-12T00:00:00Z",
    });
    const mondayMorning = pr(2, {
      ended: "2026-10-12T06:00:00Z",
      task_started: "2026-10-12T00:00:00Z",
    });

    const series = leadTimeSeries([sundayNight, mondayMorning], options);
    const total = series.stages[4];
    const filled = total.buckets.filter((b) => b.n > 0);

    // Sunday 23:00 local belongs to the week of Oct 5; Monday 01:00 to Oct 12.
    expect(filled.map((b) => b.start)).toEqual([
      localMidnight(2026, 10, 5, CHICAGO),
      localMidnight(2026, 10, 12, CHICAGO),
    ]);
  });

  test("a bucket with no PR with the stage known is a gap, and unknowns are counted", () => {
    const known = pr(1, { task_started: "2026-10-01T10:00:00Z", ended: "2026-10-02T10:00:00Z" });
    const unknown = pr(2, { ended: "2026-10-03T10:00:00Z" });
    const later = pr(3, { task_started: "2026-10-13T10:00:00Z", ended: "2026-10-14T10:00:00Z" });

    const total = leadTimeSeries([known, unknown, later], options).stages[4];
    const weekOf = (month: number, day: number) =>
      total.buckets.find((b) => b.start === localMidnight(2026, month, day, CHICAGO))!;

    expect(weekOf(9, 28)).toMatchObject({ n: 1, unknown: 1, median: 24 * HOUR });
    expect(weekOf(10, 5)).toMatchObject({ n: 0, unknown: 0, median: null, p75: null });
    expect(weekOf(10, 12)).toMatchObject({ n: 1, median: 24 * HOUR });
    expect(total.summary).toMatchObject({ n: 2, unknown: 1, median: 24 * HOUR });
    expect(total.points.map((p) => p.pr.number)).toEqual([1, 3]);
  });

  test("only PRs merged inside the window count; open and closed PRs never do", () => {
    const before = pr(1, { ended: "2026-09-20T10:00:00Z", task_started: "2026-09-19T10:00:00Z" });
    const open = pr(2, { state: "OPEN", ended: null, task_started: "2026-10-01T10:00:00Z" });
    const closed = pr(3, { state: "CLOSED", task_started: "2026-10-01T10:00:00Z" });
    const edge = pr(4, {
      ended: new Date(WINDOW_START).toISOString(),
      task_started: "2026-09-20T10:00:00Z",
    });

    const series = leadTimeSeries([before, open, closed, edge], options);

    expect(series.merged).toBe(1);
    expect(series.stages[4].points.map((p) => p.pr.number)).toEqual([4]);
  });

  test("medians and percentiles are per bucket, with the window's own summary", () => {
    const merges = [10, 20, 30, 40].map((hours, i) =>
      pr(i + 1, {
        task_started: "2026-10-06T10:00:00Z",
        ended: new Date(Date.parse("2026-10-06T10:00:00Z") + hours * HOUR * 1000).toISOString(),
      }),
    );

    const total = leadTimeSeries(merges, options).stages[4];
    const bucket = total.buckets.find((b) => b.n > 0)!;

    expect(bucket).toMatchObject({ n: 4, median: 25 * HOUR });
    expect(bucket.p75).toBeCloseTo(32.5 * HOUR);
    expect(bucket.p90).toBeCloseTo(37 * HOUR);
    expect(total.summary.median).toBe(25 * HOUR);
  });

  test("excluding weekends changes the durations and therefore the medians", () => {
    const fridayToMonday = pr(1, {
      task_started: "2026-10-09T22:00:00Z", // Fri 17:00 CDT
      ended: "2026-10-12T14:00:00Z", // Mon 09:00 CDT
    });

    const all = leadTimeSeries([fridayToMonday], options).stages[4];
    const weekdays = leadTimeSeries([fridayToMonday], { ...options, excludeWeekends: true }).stages[4];

    expect(all.summary.median).toBe(64 * HOUR);
    expect(weekdays.summary.median).toBe(16 * HOUR);
  });

  test("the bars group by day for 4 weeks, by week up to 26 and by two weeks beyond; per-merge events remain available", () => {
    expect([4, 12, 26, 52].map(bucketDays)).toEqual([1, 7, 7, 14]);
    const sameDay = [
      pr(1, { task_started: "2026-10-08T10:00:00Z", ended: "2026-10-08T12:00:00Z" }),
      pr(2, { task_started: "2026-10-08T11:00:00Z", ended: "2026-10-08T13:00:00Z" }),
    ];
    const events = leadTimeSeries(sameDay, { ...options, days: null });
    expect(events.granularity).toBe("event");
    expect(events.buckets.map((b) => b.start)).toEqual(sameDay.map((p) => Date.parse(p.ended as string)));
    expect(events.stages[4].buckets.map((b) => b.n)).toEqual([1, 1]);
    const weekly = leadTimeSeries(sameDay, options);
    expect(weekly.granularity).toBe("bucket");
    expect(weekly.stages[4].buckets.filter((b) => b.n > 0)).toHaveLength(1);
  });
});
