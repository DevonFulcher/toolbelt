import { describe, expect, test } from "vitest";
import {
  bandPath,
  countTicks,
  durationAxis,
  formatBucketDate,
  formatBucketRange,
  formatDuration,
  formatInstant,
  labelIndexes,
  linePath,
  linearScale,
  medianPositive,
  nearestIndex,
  niceMax,
  rangePath,
  dayLabels,
  weekendSpans,
  stackShares,
  timeAxis,
} from "./aggregateChart";

describe("scales", () => {
  test("linear maps the domain onto the range, and a flat domain to the start", () => {
    const scale = linearScale([0, 10], [100, 200]);
    expect([scale(0), scale(5), scale(10)]).toEqual([100, 150, 200]);
    expect(linearScale([3, 3], [0, 50])(3)).toBe(0);
  });
});

describe("ticks", () => {
  test("niceMax rounds counts up to 1, 2, 5 or 10 times a power of ten", () => {
    expect([0, 1, 3, 7, 11, 23, 70, 101].map(niceMax)).toEqual([1, 1, 5, 10, 20, 50, 100, 200]);
  });

  test("count ticks are whole numbers from zero", () => {
    expect(countTicks(0)).toEqual([0, 1]);
    expect(countTicks(7)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(countTicks(23)).toEqual([0, 10, 20, 30, 40, 50]);
    for (const t of countTicks(37)) expect(Number.isInteger(t)).toBe(true);
  });

  test("label indexes spread evenly and include the first", () => {
    expect(labelIndexes(3, 6)).toEqual([0, 1, 2]);
    const picked = labelIndexes(84, 6);
    expect(picked[0]).toBe(0);
    expect(picked.length).toBeLessThanOrEqual(7);
    expect(picked.every((v, i) => i === 0 || v > picked[i - 1])).toBe(true);
  });
});

describe("stacking", () => {
  test("bands are cumulative and a bucket without data is a gap in every band", () => {
    const bands = stackShares([
      [0.25, null, 1],
      [0.75, null, 0],
    ]);

    expect(bands[0].spans).toEqual([[0, 0.25], null, [0, 1]]);
    expect(bands[1].spans).toEqual([[0.25, 1], null, [1, 1]]);
  });

  test("the top of the last band is 1 for every bucket that has data", () => {
    const shares = [
      [0.2, 0.5],
      [0.3, 0.1],
      [0.5, 0.4],
    ];
    const bands = stackShares(shares);

    for (let b = 0; b < 2; b++) {
      expect(bands[2].spans[b]![1]).toBeCloseTo(1);
    }
  });

  test("a band's path is constant between transition instants", () => {
    const band = { spans: [[0, 0.5], [0, 0.5], null, [0.5, 1]] as ([number, number] | null)[] };
    const path = bandPath(
      band,
      (i) => i * 10,
      (share) => 100 - share * 100,
      4,
    );

    expect(path).toBe("M0,50L10,50L10,100L0,100Z" + "M10,50L20,50L20,100L10,100Z" + "M30,0L34,0L34,50L30,50Z");
  });
});

describe("durations", () => {
  test("format with one decimal at most", () => {
    expect(formatDuration(30)).toBe("<1m");
    expect(formatDuration(40 * 60)).toBe("40m");
    expect(formatDuration(5.5 * 3600)).toBe("5.5h");
    expect(formatDuration(2.25 * 86400)).toBe("2.3d");
    expect(formatDuration(3 * 7 * 86400)).toBe("3w");
  });

  test("the axis starts at 0 and picks minutes, hours or days from the range", () => {
    const minutes = durationAxis(40 * 60);
    const hours = durationAxis(30 * 3600);
    const days = durationAxis(9 * 86400);

    expect([minutes.unit, hours.unit, days.unit]).toEqual(["m", "h", "d"]);
    expect(minutes.ticks[0]).toEqual({ value: 0, label: "0m" });
    expect(minutes.top).toBeGreaterThanOrEqual(40 * 60);
    expect(hours.ticks.map((t) => t.label)).toEqual(["0h", "10h", "20h", "30h", "40h", "50h"]);
    expect(days.top).toBe(10 * 86400);
    expect(days.ticks.every((t) => Number.isInteger(t.value / 86400))).toBe(true);
  });

  test("an empty axis still has a scale", () => {
    expect(durationAxis(0).ticks.length).toBeGreaterThanOrEqual(2);
  });
});

describe("ranges", () => {
  test("a band is one closed shape per run of buckets and breaks at a gap", () => {
    const path = rangePath(
      [1, 2, null, 3],
      [2, 4, 5, 6],
      (i) => i * 10,
      (v) => 100 - v * 10,
      4,
    );

    expect(path).toBe("M0,80L10,60L10,80L0,90Z" + "M28,40L32,40L32,70L28,70Z");
  });
});

describe("day labels", () => {
  const monday = Date.UTC(2026, 9, 5);
  const days = Array.from({ length: 7 }, (_, i) => ({ start: monday + i * 86_400_000 }));

  test("wide bars name the day, Mondays carry the date", () => {
    expect(dayLabels(days, "UTC", 50)).toEqual(["Oct 5", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]);
  });

  test("narrower bars use initials, and the narrowest only label Mondays", () => {
    expect(dayLabels(days, "UTC", 36)).toEqual(["Oct 5", "T", "W", "T", "F", "S", "S"]);
    expect(dayLabels(days, "UTC", 20)).toEqual(["Oct 5", null, null, null, null, null, null]);
    expect(dayLabels(days, "UTC", 6)).toEqual(["Oct 5", null, null, null, null, null, null]);
  });

  test("when bars are so narrow that dates would collide, only every few Mondays are labelled", () => {
    const four = Array.from({ length: 28 }, (_, i) => ({ start: monday + i * 86_400_000 }));

    const labels = dayLabels(four, "UTC", 3).filter((label) => label !== null);

    // 7 bars of 3px are 21px apart, a date needs 48: every third Monday.
    expect(labels).toEqual(["Oct 5", "Oct 26"]);
    expect(dayLabels(four, "UTC", 5).filter((l) => l !== null)).toEqual(["Oct 5", "Oct 19"]);
  });
});

describe("weekend bands", () => {
  test("weekend time collapses to one x position, including a daylight-saving change", () => {
    const zone = "America/Chicago";
    const friday = Date.parse("2026-10-30T17:00:00Z"); // Friday noon CDT
    const saturday = Date.parse("2026-10-31T17:00:00Z");
    const sunday = Date.parse("2026-11-01T18:00:00Z"); // noon CST, after the clock change
    const monday = Date.parse("2026-11-02T18:00:00Z"); // Monday noon CST
    const axis = timeAxis(friday, monday, zone, true);
    expect(axis(friday)).toBe(0);
    expect(axis(saturday)).toBe(0.5);
    expect(axis(sunday)).toBe(0.5);
    expect(axis(monday)).toBe(1);
    expect(timeAxis(friday, monday, zone, false)(sunday)).toBeGreaterThan(0.5);
  });

  test("Saturday and Sunday merge into one band, clipped to the range", () => {
    const monday = Date.UTC(2026, 9, 5);
    const day = 86_400_000;

    expect(weekendSpans(monday, monday + 14 * day, "UTC")).toEqual([
      [monday + 5 * day, monday + 7 * day],
      [monday + 12 * day, monday + 14 * day],
    ]);
    // Starting on a Sunday afternoon: only the rest of that Sunday.
    expect(weekendSpans(monday + 6.5 * day, monday + 9 * day, "UTC")).toEqual([
      [monday + 6.5 * day, monday + 7 * day],
    ]);
  });

  test("follow the viewer's zone, so Friday evening in Chicago is not a weekend", () => {
    const friday7pmChicago = Date.UTC(2026, 9, 10, 0, 0); // Saturday 00:00 UTC
    const end = friday7pmChicago + 86_400_000;

    const utc = weekendSpans(friday7pmChicago, end, "UTC");
    const chicago = weekendSpans(friday7pmChicago, end, "America/Chicago");

    expect(utc).toEqual([[friday7pmChicago, end]]);
    // Chicago: the weekend starts at its Saturday 00:00 (05:00 UTC).
    expect(chicago).toEqual([[Date.UTC(2026, 9, 10, 5), end]]);
  });
});

describe("dates", () => {
  test("buckets are dated in UTC", () => {
    expect(formatBucketDate("2026-10-05T00:00:00Z")).toBe("Oct 5");
    expect(formatBucketRange("2026-10-05T00:00:00Z", 7)).toBe("Oct 5 - Oct 11");
    expect(formatBucketRange("2026-10-05T00:00:00Z", 1)).toBe("Oct 5");
  });
});

describe("lines over sampled instants", () => {
  test("a line breaks at a gap instead of being drawn across it", () => {
    const path = linePath(
      [0.5, 0.25, null, null, 1],
      (i) => i * 10,
      (v) => 100 - v * 100,
    );

    expect(path).toBe("M0,50L10,75" + "M40,0L40,0");
  });

  test("all gaps draw nothing", () => {
    expect(linePath([null, null], (i) => i, (v) => v)).toBe("");
  });

  test("the nearest sample to a pointer time", () => {
    const times = [0, 10, 20, 40];

    expect([nearestIndex(times, -5), nearestIndex(times, 4), nearestIndex(times, 16)]).toEqual([0, 0, 2]);
    expect([nearestIndex(times, 31), nearestIndex(times, 29), nearestIndex(times, 99)]).toEqual([3, 2, 3]);
    expect(nearestIndex([], 5)).toBe(-1);
  });

  test("instants are written in UTC to the minute", () => {
    expect(formatInstant("2026-10-05T06:00:00Z")).toBe("Oct 5, 06:00");
  });

  test("the typical number in flight ignores empty instants", () => {
    expect(medianPositive([0, 0, 2, 4, 0, 3])).toBe(3);
    expect(medianPositive([0, 0])).toBe(0);
  });
});
