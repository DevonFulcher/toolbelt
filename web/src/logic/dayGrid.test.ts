import { describe, expect, test } from "vitest";
import { dayGrid, gridDensity, visibleDays } from "./dayGrid";
import { DAY, HOUR } from "./format";

const at = (iso: string): number => Date.parse(iso);
const iso = (ms: number): string => new Date(ms).toISOString();

describe("day grid", () => {
  test("lines at local midnights, weekdays labelled", () => {
    // Wed 2026-10-14 15:00 UTC to Sat 2026-10-17 03:00 UTC, in UTC.
    const start = at("2026-10-14T15:00:00Z");
    const end = at("2026-10-17T03:00:00Z");

    const { days } = dayGrid(start, end, "UTC");

    expect(days.map((d) => [iso(d.t), d.label])).toEqual([
      ["2026-10-15T00:00:00.000Z", "Thu"],
      ["2026-10-16T00:00:00.000Z", "Fri"],
      ["2026-10-17T00:00:00.000Z", "Sat"],
    ]);
    expect(days[0].x).toBeCloseTo(9 / 60);
    expect(days[2].x).toBeCloseTo(57 / 60);
  });

  test("the same instants fall on different days in different timezones", () => {
    const start = at("2026-10-14T12:00:00Z");
    const end = at("2026-10-16T12:00:00Z");

    const utc = dayGrid(start, end, "UTC").days.map((d) => iso(d.t));
    const chicago = dayGrid(start, end, "America/Chicago").days.map((d) => iso(d.t));

    expect(utc).toEqual(["2026-10-15T00:00:00.000Z", "2026-10-16T00:00:00.000Z"]);
    // CDT is UTC-5: midnight is 05:00Z.
    expect(chicago).toEqual(["2026-10-15T05:00:00.000Z", "2026-10-16T05:00:00.000Z"]);
  });

  test("spring forward: a 23-hour calendar day, not 24h multiples", () => {
    // Fri 2026-03-06 12:00 CST .. Wed 2026-03-11 12:00 CDT; DST began Sun 03-08 at 02:00.
    const start = at("2026-03-06T18:00:00Z");
    const end = at("2026-03-11T17:00:00Z");

    const { days } = dayGrid(start, end, "America/Chicago");

    expect(days.map((d) => [iso(d.t), d.label])).toEqual([
      ["2026-03-07T06:00:00.000Z", "Sat"],
      ["2026-03-08T06:00:00.000Z", "Sun"],
      ["2026-03-09T05:00:00.000Z", "Mon"],
      ["2026-03-10T05:00:00.000Z", "Tue"],
      ["2026-03-11T05:00:00.000Z", "Wed"],
    ]);
    expect(days[2].t - days[1].t).toBe(23 * HOUR);
  });

  test("fall back: a 25-hour calendar day", () => {
    // DST ended Sun 2026-11-01 at 02:00.
    const start = at("2026-10-31T18:00:00Z");
    const end = at("2026-11-03T18:00:00Z");

    const { days } = dayGrid(start, end, "America/Chicago");

    expect(days.map((d) => [iso(d.t), d.label])).toEqual([
      ["2026-11-01T05:00:00.000Z", "Sun"],
      ["2026-11-02T06:00:00.000Z", "Mon"],
      ["2026-11-03T06:00:00.000Z", "Tue"],
    ]);
    expect(days[1].t - days[0].t).toBe(25 * HOUR);
  });

  test("a weekend is Saturday 00:00 to Monday 00:00, one span across the DST change", () => {
    const start = at("2026-03-06T18:00:00Z");
    const end = at("2026-03-11T17:00:00Z");

    const { weekends } = dayGrid(start, end, "America/Chicago");

    expect(weekends.map((w) => [iso(w.t0), iso(w.t1)])).toEqual([
      ["2026-03-07T06:00:00.000Z", "2026-03-09T05:00:00.000Z"],
    ]);
    expect(weekends[0].t1 - weekends[0].t0).toBe(47 * HOUR);
  });

  test("a 3-hour track has no lines unless it crosses midnight", () => {
    const quiet = dayGrid(at("2026-10-14T10:00:00Z"), at("2026-10-14T13:00:00Z"), "UTC");
    expect(quiet.days).toEqual([]);
    expect(quiet.weekends).toEqual([]);

    const crossing = dayGrid(at("2026-10-14T22:30:00Z"), at("2026-10-15T01:30:00Z"), "UTC");
    expect(crossing.days).toHaveLength(1);
    expect(crossing.days[0].x).toBeCloseTo(0.5);
  });

  test("a short track inside a weekend is entirely banded", () => {
    // Saturday 2026-10-17, 10:00-13:00 UTC.
    const { weekends, days } = dayGrid(at("2026-10-17T10:00:00Z"), at("2026-10-17T13:00:00Z"), "UTC");

    expect(days).toEqual([]);
    expect(weekends).toEqual([
      { t0: at("2026-10-17T10:00:00Z"), t1: at("2026-10-17T13:00:00Z"), x0: 0, x1: 1 },
    ]);
  });

  test("a track starting on a Sunday: the weekend is clipped to its start", () => {
    // Sun 2026-10-18 09:00 to Tue 2026-10-20 09:00 UTC.
    const start = at("2026-10-18T09:00:00Z");
    const end = at("2026-10-20T09:00:00Z");

    const { days, weekends } = dayGrid(start, end, "UTC");

    expect(weekends).toHaveLength(1);
    expect(weekends[0].t0).toBe(start);
    expect(iso(weekends[0].t1)).toBe("2026-10-19T00:00:00.000Z");
    expect(weekends[0].x0).toBe(0);
    expect(weekends[0].x1).toBeCloseTo(15 / 48);
    expect(days.map((d) => d.label)).toEqual(["Mon", "Tue"]);
  });

  test("a track ending on a Saturday morning: the weekend is clipped to its end", () => {
    const start = at("2026-10-15T09:00:00Z");
    const end = at("2026-10-17T06:00:00Z");

    const { weekends } = dayGrid(start, end, "UTC");

    expect(weekends).toHaveLength(1);
    expect(iso(weekends[0].t0)).toBe("2026-10-17T00:00:00.000Z");
    expect(weekends[0].t1).toBe(end);
    expect(weekends[0].x1).toBe(1);
  });

  test("six weeks: 42 midnights, 6 or 7 weekends, 6 Mondays", () => {
    // Mon 2026-09-07 00:00 UTC exactly to Mon 2026-10-19 00:00 UTC.
    const start = at("2026-09-07T00:00:00Z");
    const end = start + 42 * DAY;

    const { days, weekends } = dayGrid(start, end, "UTC");

    // The start itself is on the track's edge and the end is not "inside".
    expect(days).toHaveLength(41);
    expect(days.filter((d) => d.weekday === 1)).toHaveLength(5);
    expect(weekends).toHaveLength(6);
    for (const w of weekends) expect(w.t1 - w.t0).toBe(2 * DAY);
    for (const day of days) {
      expect(day.x).toBeGreaterThan(0);
      expect(day.x).toBeLessThan(1);
    }
  });
});

describe("grid density", () => {
  const week = 7 * DAY;

  test("every day with full labels when there is room", () => {
    expect(gridDensity(700, week)).toEqual({ lines: "days", letters: 3 });
  });

  test("initials between 14 and 26 px per day", () => {
    expect(gridDensity(100, 7 * DAY)).toEqual({ lines: "days", letters: 1 });
  });

  test("only Mondays under 14 px per day", () => {
    const grid = dayGrid(at("2026-09-07T00:00:00Z"), at("2026-09-07T00:00:00Z") + 21 * DAY, "UTC");
    const density = gridDensity(200, 21 * DAY);

    expect(density).toEqual({ lines: "weeks", letters: 3 });
    expect(visibleDays(grid, density).map((d) => d.label)).toEqual(["Mon", "Mon"]);
  });

  test("only bands under 5 px per day", () => {
    const grid = dayGrid(at("2026-07-01T00:00:00Z"), at("2026-07-01T00:00:00Z") + 120 * DAY, "UTC");
    const density = gridDensity(300, 120 * DAY);

    expect(density.lines).toBe("none");
    expect(visibleDays(grid, density)).toEqual([]);
    expect(grid.weekends.length).toBeGreaterThan(10);
  });

  test("a track under a day counts as dense-enough", () => {
    expect(gridDensity(300, 3 * HOUR).lines).toBe("days");
  });
});
