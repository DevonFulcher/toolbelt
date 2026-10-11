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
  logScale,
  medianPositive,
  nearestIndex,
  niceMax,
  packStrip,
  rowOffset,
  stackShares,
} from "./aggregateChart";

describe("scales", () => {
  test("linear maps the domain onto the range, and a flat domain to the start", () => {
    const scale = linearScale([0, 10], [100, 200]);
    expect([scale(0), scale(5), scale(10)]).toEqual([100, 150, 200]);
    expect(linearScale([3, 3], [0, 50])(3)).toBe(0);
  });

  test("log puts each decade the same distance apart and clamps below the domain", () => {
    const scale = logScale([1, 1000], [0, 300]);
    expect([scale(1), scale(10), scale(100), scale(1000)]).toEqual([0, 100, 200, 300]);
    expect(scale(0)).toBe(0);
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

  test("a band's path is one closed area per run, and a lone bucket is a column", () => {
    const band = { spans: [[0, 0.5], [0, 0.5], null, [0.5, 1]] as ([number, number] | null)[] };
    const path = bandPath(
      band,
      (i) => i * 10,
      (share) => 100 - share * 100,
      4,
    );

    expect(path).toBe("M0,50L10,50L10,100L0,100Z" + "M28,0L32,0L32,50L28,50Z");
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

  test("the axis spans the ladder rungs around the data", () => {
    const axis = durationAxis([2 * 3600, 5 * 86400]);

    expect(axis.domain).toEqual([3600, 7 * 86400]);
    expect(axis.ticks).toEqual([3600, 4 * 3600, 12 * 3600, 86400, 3 * 86400, 7 * 86400]);
  });

  test("an empty or single-valued axis still has two rungs", () => {
    expect(durationAxis([]).ticks.length).toBeGreaterThanOrEqual(2);
    expect(durationAxis([86400]).ticks.length).toBeGreaterThanOrEqual(2);
  });
});

describe("strips", () => {
  test("dots that would overlap go to different rows, far ones share a row", () => {
    const { rows, used } = packStrip([10, 12, 40, 100], 6, 5);

    expect(rows).toEqual([0, 1, 0, 0]);
    expect(used).toBe(2);
  });

  test("rows alternate above and below the middle", () => {
    expect([0, 1, 2, 3, 4].map((r) => rowOffset(r, 6))).toEqual([0, -6, 6, -12, 12]);
  });

  test("the number of rows is capped", () => {
    const { used } = packStrip([1, 1, 1, 1, 1, 1, 1], 6, 3);

    expect(used).toBe(3);
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
