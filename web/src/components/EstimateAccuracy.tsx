// The finish estimate's track record in the aggregate view: coverage and
// median error over the stored backtests, the parameters in use, and a
// regime change when stage durations moved.
import type { EstimateAccuracy as Accuracy } from "../api/generated";
import {
  TARGET_COVERAGE,
  backtestSeries,
  formatWorking,
  percent,
  regimeText,
  stagesByVolume,
} from "../logic/finishEstimate";
import { formatLocalInstant } from "../logic/localTime";

const WIDTH = 520;
const HEIGHT = 120;
const PAD = 28;

function Coverage({ points }: { points: ReturnType<typeof backtestSeries> }) {
  const known = points.filter((p) => p.coverage !== null);
  if (known.length < 2) return null;
  const first = known[0]!.ranAt;
  const span = Math.max(1, known[known.length - 1]!.ranAt - first);
  const x = (t: number) => PAD + ((WIDTH - 2 * PAD) * (t - first)) / span;
  const y = (v: number) => HEIGHT - PAD - (HEIGHT - 2 * PAD) * v;
  const line = known.map((p) => `${x(p.ranAt).toFixed(1)},${y(p.coverage ?? 0).toFixed(1)}`).join(" ");
  return (
    <svg role="img" aria-label="Coverage of the p80 over time" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width="100%" style={{ maxWidth: WIDTH }}>
      <line className="agg-grid" x1={PAD} x2={WIDTH - PAD} y1={y(TARGET_COVERAGE)} y2={y(TARGET_COVERAGE)} strokeDasharray="4 3" />
      <text className="agg-tick" x={WIDTH - PAD} y={y(TARGET_COVERAGE) - 4} textAnchor="end">
        target 80%
      </text>
      <line className="agg-axis" x1={PAD} x2={WIDTH - PAD} y1={y(0)} y2={y(0)} />
      <text className="agg-tick" x={4} y={y(1) + 4}>
        100%
      </text>
      <text className="agg-tick" x={4} y={y(0)}>
        0%
      </text>
      <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth={2} />
    </svg>
  );
}

export function EstimateAccuracySection({ accuracy, timeZone }: { accuracy: Accuracy; timeZone: string }) {
  const series = backtestSeries(accuracy);
  const latest = accuracy.backtests[accuracy.backtests.length - 1];
  const day = accuracy.working_day_seconds;
  return (
    <section className="agg-chart agg-estimate" aria-label="Estimate accuracy">
      <header>
        <h2>Estimate accuracy</h2>
      </header>
      <p className="agg-description">
        Each night the estimate is replayed over the finished tasks: at every stage boundary it predicts from only what
        was known before, and the actual finish is compared with its p80 (it should be at or under it 80% of the time) and
        p50.
      </p>
      {latest === undefined ? (
        <p className="agg-description">No backtest yet: there is not enough finished work in the history, or it has not run.</p>
      ) : (
        <>
          <p>
            {`Last backtest ${formatLocalInstant(Date.parse(latest.ran_at), timeZone)}: ${percent(latest.overall.coverage)} of ${latest.overall.n - latest.overall.unknown} predictions finished at or under their p80 (target ${percent(TARGET_COVERAGE)}), median error ${latest.overall.median_abs_error_seconds === null ? "–" : formatWorking(latest.overall.median_abs_error_seconds, day)}, p50 to p80 range ${latest.overall.median_width_seconds === null ? "–" : formatWorking(latest.overall.median_width_seconds, day)}; ${latest.overall.unknown} unknown.`}
          </p>
          <p className="agg-description">
            {accuracy.current === null
              ? "Using the default parameters."
              : `Parameters in use: half-life ${accuracy.current.half_life_weeks} weeks, at least ${accuracy.current.min_samples} samples per group, shrinkage ${accuracy.current.shrinkage}.`}
          </p>
          {latest.regime.map((change) => (
            <p key={change.stage} className="notice">
              {`${regimeText(change.stage, change.change, change.weeks)} (median ${formatWorking(change.prior_median_seconds, day)} to ${formatWorking(change.recent_median_seconds, day)}, ${change.n_prior} and ${change.n_recent} samples).`}
            </p>
          ))}
          <Coverage points={series} />
          <table className="agg-estimate-stages">
            <caption className="agg-description">Latest backtest by stage</caption>
            <thead>
              <tr>
                <th>Stage</th>
                <th>Predictions</th>
                <th>Unknown</th>
                <th>Coverage</th>
                <th>Median error</th>
                <th>Range (p50 to p80)</th>
              </tr>
            </thead>
            <tbody>
              {stagesByVolume(latest.by_stage).map((stage) => (
                <tr key={stage.stage}>
                  <td>{stage.stage.replace(/_/g, " ")}</td>
                  <td>{stage.n}</td>
                  <td>{stage.unknown}</td>
                  <td>{percent(stage.coverage)}</td>
                  <td>{stage.median_abs_error_seconds === null ? "–" : formatWorking(stage.median_abs_error_seconds, day)}</td>
                  <td>{stage.median_width_seconds === null ? "–" : formatWorking(stage.median_width_seconds, day)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <p className="agg-description">
        {accuracy.live_finished === 0
          ? `${accuracy.live_made} estimate${accuracy.live_made === 1 ? "" : "s"} shown so far; none of those tasks has finished yet.`
          : `Of ${accuracy.live_made} estimates shown, ${accuracy.live_finished} tasks have finished: ${percent(accuracy.live_coverage)} of them at or before their p80.`}
      </p>
    </section>
  );
}
