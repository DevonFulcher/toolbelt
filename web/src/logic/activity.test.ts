import { describe, expect, test } from "vitest";
import type { AggregateActivity } from "../api/generated";
import { activityBuckets, formatDuration } from "./activity";

const hour = (t: string, ms: number, category = "test"): AggregateActivity["hours"][number] => ({
  t,
  turns: 1,
  calls: 2,
  wall_ms: ms * 2,
  tool_ms: ms,
  delegated_ms: 0,
  blocked_ms: 0,
  model_ms: ms,
  tools: [{ name: "Bash", calls: 2, ms }],
  categories: [{ name: category, calls: 2, ms }],
});

const window = {
  windowStart: Date.parse("2026-10-05T00:00:00Z"),
  now: Date.parse("2026-10-08T12:00:00Z"),
  weeks: 12,
  timeZone: "UTC",
};

describe("activityBuckets", () => {
  test("sums the server's hours into the viewer's local days", () => {
    const result = activityBuckets(
      { since: "2026-10-05T00:00:00Z", hours: [hour("2026-10-05T01:00:00Z", 10), hour("2026-10-05T23:00:00Z", 5), hour("2026-10-06T00:00:00Z", 7)] },
      window,
    );

    expect(result.model.slice(0, 3)).toEqual([15, 7, 0]);
    expect(result.totals).toMatchObject({ turns: 3, calls: 6, toolMs: 22, modelMs: 22 });
  });

  test("a time zone moves an hour to the day it is local to", () => {
    // 23:00 UTC on the 5th is the 6th in Tokyo.
    const result = activityBuckets(
      { since: "x", hours: [hour("2026-10-05T23:00:00Z", 5)] },
      { ...window, timeZone: "Asia/Tokyo", windowStart: Date.parse("2026-10-04T15:00:00Z") },
    );

    expect(result.model.slice(0, 3)).toEqual([0, 5, 0]);
  });

  test("a longer window makes weeks", () => {
    const result = activityBuckets(
      { since: "x", hours: [hour("2026-10-05T01:00:00Z", 10), hour("2026-10-09T01:00:00Z", 5)] },
      { ...window, weeks: 26, now: Date.parse("2026-10-20T00:00:00Z") },
    );

    expect(result.model.slice(0, 2)).toEqual([15, 0]);
  });

  test("lists categories and tools biggest first and leaves out hours outside the window", () => {
    const result = activityBuckets(
      {
        since: "x",
        hours: [hour("2026-10-05T01:00:00Z", 10, "vcs"), hour("2026-10-05T02:00:00Z", 30, "test"), hour("2026-09-01T00:00:00Z", 99)],
      },
      window,
    );

    expect(result.categories.map((c) => [c.name, c.total])).toEqual([
      ["test", 30],
      ["vcs", 10],
    ]);
    expect(result.tools).toEqual([{ name: "Bash", calls: 4, ms: 40 }]);
  });
});

describe("formatDuration", () => {
  test.each([
    [12_000, "12s"],
    [250_000, "4m 10s"],
    [7_500_000, "2h 05m"],
    [0, "0s"],
  ])("%d ms is %s", (ms, text) => {
    expect(formatDuration(ms)).toBe(text);
  });
});
