import { describe, expect, test } from "vitest";
import type { CardEstimate, EstimateAccuracy } from "../api/generated";
import { backtestSeries, estimateText, formatWorking, percent, regimeText, stagesByVolume } from "./finishEstimate";

const NOW = Date.parse("2026-10-26T15:00:00Z");

function estimate(patch: Partial<CardEstimate> = {}): CardEstimate {
  return {
    known: true,
    stage: "wait_review",
    finish_p50: "2026-10-28T15:00:00Z",
    finish_p80: "2026-10-30T15:00:00Z",
    overdue: false,
    partial: false,
    level: "reviewer",
    n: 14,
    explanation: "waiting on ann, 2.1 working days in, based on 14 reviews",
    model: "baseline",
    ...patch,
  };
}

describe("estimateText", () => {
  test("shows the median and the p80 from now, the explanation in the tooltip", () => {
    const text = estimateText(estimate(), NOW, "UTC");
    expect(text.text).toBe("~2d (p50), up to 4d (p80)");
    expect(text.known).toBe(true);
    expect(text.title).toContain("waiting on ann, 2.1 working days in, based on 14 reviews");
    expect(text.title).toContain("p50: Wed Oct 28, 15:00");
    expect(text.title).toContain("Level: reviewer, 14 samples");
  });

  test("minutes and hours for something close", () => {
    const text = estimateText(
      estimate({ finish_p50: "2026-10-26T15:45:00Z", finish_p80: "2026-10-26T19:00:00Z" }),
      NOW,
      "UTC",
    );
    expect(text.text).toBe("~45m (p50), up to 4h (p80)");
  });

  test("an instant already past is not shown as negative", () => {
    expect(estimateText(estimate({ finish_p50: "2026-10-26T10:00:00Z" }), NOW, "UTC").text).toContain("~<1m");
  });

  test("unknown says why and shows no number", () => {
    const text = estimateText(
      estimate({ known: false, finish_p50: null, finish_p80: null, explanation: "unknown: not enough history (2 of the 5 wait_review stages needed)" }),
      NOW,
      "UTC",
    );
    expect(text).toEqual({
      text: "unknown: not enough history",
      title: "unknown: not enough history (2 of the 5 wait_review stages needed)",
      known: false,
    });
  });

  test("overdue and partial are said", () => {
    const text = estimateText(estimate({ overdue: true, partial: true }), NOW, "UTC").text;
    expect(text).toBe("overdue, ~2d (p50), up to 4d (p80) (not counting later stages)");
  });

  test("the tooltip's instants are in the viewer's zone", () => {
    expect(estimateText(estimate(), NOW, "America/Chicago").title).toContain("p50: Wed Oct 28, 10:00  p80: Fri Oct 30, 10:00 (America/Chicago)");
  });
});

describe("formatWorking", () => {
  test("minutes, hours and working days", () => {
    const day = 8 * 3600;
    expect(formatWorking(10, day)).toBe("1m");
    expect(formatWorking(30 * 60, day)).toBe("30m");
    expect(formatWorking(3.5 * 3600, day)).toBe("3.5h");
    expect(formatWorking(12 * 3600, day)).toBe("1.5 working days");
  });
});

test("percent and regime text", () => {
  expect(percent(0.794)).toBe("79%");
  expect(percent(null)).toBe("–");
  expect(regimeText("wait_review", -0.45, 4)).toBe("wait review estimates are now 45% shorter than 4 weeks ago");
  expect(regimeText("merge", 0.3, 4)).toBe("merge estimates are now 30% longer than 4 weeks ago");
});

function stage(name: string, n: number) {
  return { stage: name, n, unknown: 0, coverage: 0.8, median_abs_error_seconds: 1, median_width_seconds: 1, censored: 0, coverage_adjusted: 0.8, loss: 1 };
}

test("the series follows the stored backtests and stages are ordered by volume", () => {
  const params = { half_life_weeks: 12, min_samples: 5, shrinkage: 5 };
  const accuracy: EstimateAccuracy = {
    working_day_seconds: 28800,
    current: params,
    backtests: [
      { ran_at: "2026-10-01T00:00:00Z", params, overall: { ...stage("all", 10), coverage: 0.7 }, by_stage: [], regime: [], models: [], champions: [], ablation_model: "", ablation: [] },
      { ran_at: "2026-10-02T00:00:00Z", params, overall: { ...stage("all", 12), coverage: null }, by_stage: [], regime: [], models: [], champions: [], ablation_model: "", ablation: [] },
    ],
    live_made: 0,
    live_finished: 0,
    live_coverage: null,
    champion_changes: [],
    fits: [],
  };
  expect(backtestSeries(accuracy).map((p) => p.coverage)).toEqual([0.7, null]);
  expect(stagesByVolume([stage("b", 2), stage("a", 2), stage("c", 9)]).map((s) => s.stage)).toEqual(["c", "a", "b"]);
});
