import { describe, expect, test } from "vitest";
import { formatElapsed } from "./format";

const M = 60;
const H = 3600;
const D = 24 * H;

describe("formatElapsed", () => {
  test("is short below an hour", () => {
    expect(formatElapsed(30)).toBe("<1m");
    expect(formatElapsed(5 * M)).toBe("5m");
  });

  test("shows the largest unit and the two below it", () => {
    expect(formatElapsed(3 * H + 12 * M)).toBe("3h 12m");
    expect(formatElapsed(D + 5 * H + 7 * M)).toBe("1d 5h 7m");
    expect(formatElapsed(2 * D + 23 * H + 59 * M + 59)).toBe("2d 23h 59m");
    expect(formatElapsed(16 * D + 20 * H + 30 * M)).toBe("2w 2d 20h");
  });

  test("leaves out a unit that is zero", () => {
    expect(formatElapsed(23 * H)).toBe("23h");
    expect(formatElapsed(2 * D + 59 * M)).toBe("2d 59m");
    expect(formatElapsed(2 * D + 4 * H)).toBe("2d 4h");
    expect(formatElapsed(21 * D)).toBe("3w");
    expect(formatElapsed(21 * D + 4 * H)).toBe("3w 4h");
  });

  test("never goes negative", () => {
    expect(formatElapsed(-5)).toBe("<1m");
  });
});
