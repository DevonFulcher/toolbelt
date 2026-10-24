// The finish estimate as text. The server sends UTC instants (it did the
// working-time arithmetic); this only subtracts and formats them.
import type { CardEstimate, EstimateAccuracy, EstimateStageAccuracy } from "../api/generated";
import { formatAge } from "./format";
import { formatLocalInstant } from "./localTime";

/** The target share of actual times at or under the p80. */
export const TARGET_COVERAGE = 0.8;

export interface EstimateText {
  /** "~2d (p50), up to 4d (p80)", or why there is none. */
  text: string;
  /** The explanation and the instants in the viewer's zone. */
  title: string;
  known: boolean;
}

function until(instant: string, now: number): string {
  return formatAge(Math.max(0, (Date.parse(instant) - now) / 1000));
}

export function estimateText(estimate: CardEstimate, now: number, timeZone: string): EstimateText {
  if (!estimate.known || estimate.finish_p50 === null || estimate.finish_p80 === null) {
    return { text: "unknown: not enough history", title: estimate.explanation, known: false };
  }
  const p50 = Date.parse(estimate.finish_p50);
  const p80 = Date.parse(estimate.finish_p80);
  const head = estimate.overdue ? "overdue, " : "";
  const partial = estimate.partial ? " (not counting later stages)" : "";
  return {
    text: `${head}~${until(estimate.finish_p50, now)} (p50), up to ${until(estimate.finish_p80, now)} (p80)${partial}`,
    title: [
      estimate.explanation,
      `p50: ${formatLocalInstant(p50, timeZone)}  p80: ${formatLocalInstant(p80, timeZone)} (${timeZone})`,
      `Level: ${estimate.level}, ${estimate.n} samples`,
    ].join("\n"),
    known: true,
  };
}

/** Working seconds as hours or working days ("3.5h", "1.2 working days"). */
export function formatWorking(seconds: number, workingDaySeconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m`;
  if (seconds < workingDaySeconds) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / workingDaySeconds).toFixed(1)} working days`;
}

export function percent(share: number | null): string {
  return share === null ? "–" : `${Math.round(share * 100)}%`;
}

/** "wait review estimates are now 45% shorter than 4 weeks ago". */
export function regimeText(stage: string, change: number, weeks: number): string {
  const size = Math.round(Math.abs(change) * 100);
  return `${stage.replace(/_/g, " ")} estimates are now ${size}% ${change < 0 ? "shorter" : "longer"} than ${weeks} weeks ago`;
}

export interface BacktestPoint {
  ranAt: number;
  coverage: number | null;
  medianAbsError: number | null;
}

/** Coverage and median error over time, oldest first. */
export function backtestSeries(accuracy: EstimateAccuracy): BacktestPoint[] {
  return accuracy.backtests.map((b) => ({
    ranAt: Date.parse(b.ran_at),
    coverage: b.overall.coverage,
    medianAbsError: b.overall.median_abs_error_seconds,
  }));
}

/** Stages with the most predictions first. */
export function stagesByVolume(stages: readonly EstimateStageAccuracy[]): EstimateStageAccuracy[] {
  return [...stages].sort((a, b) => b.n - a.n || a.stage.localeCompare(b.stage));
}

export interface ModelSeries {
  model: string;
  /** One value per stored backtest, oldest first (`null`: none that night). */
  coverage: (number | null)[];
  loss: (number | null)[];
}

/** Each model's coverage and pinball loss over the stored backtests. */
export function modelSeries(accuracy: EstimateAccuracy): ModelSeries[] {
  const names = [...new Set(accuracy.backtests.flatMap((b) => b.models.map((m) => m.model)))].sort((a, b) =>
    a === "baseline" ? -1 : b === "baseline" ? 1 : a.localeCompare(b),
  );
  return names.map((model) => {
    const overall = accuracy.backtests.map((b) => b.models.find((m) => m.model === model)?.overall ?? null);
    return {
      model,
      coverage: overall.map((o) => o?.coverage ?? null),
      loss: overall.map((o) => o?.loss ?? null),
    };
  });
}
