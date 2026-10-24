// The finish estimate's track record in the aggregate view: coverage and
// median error over the stored backtests, the parameters in use, and a
// regime change when stage durations moved.
import type { EstimateAccuracy as Accuracy } from "../api/generated";
import {
  TARGET_COVERAGE,
  backtestSeries,
  modelSeries,
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

const PALETTE = ["var(--accent)", "#d9822b", "#3fa36b", "#b05fc4"];

function ModelLines({ accuracy }: { accuracy: Accuracy }) {
  const series = modelSeries(accuracy);
  const values = series.flatMap((s) => s.loss.filter((v): v is number => v !== null));
  if (series.length < 2 || accuracy.backtests.length < 2 || values.length === 0) return null;
  const top = Math.max(...values) * 1.05;
  const n = accuracy.backtests.length;
  const x = (i: number) => PAD + ((WIDTH - 2 * PAD) * i) / (n - 1);
  const y = (v: number) => HEIGHT - PAD - ((HEIGHT - 2 * PAD) * v) / top;
  return (
    <figure className="agg-model-lines">
      <svg role="img" aria-label="Pinball loss by model over time" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width="100%" style={{ maxWidth: WIDTH }}>
        <line className="agg-axis" x1={PAD} x2={WIDTH - PAD} y1={y(0)} y2={y(0)} />
        {series.map((s, index) => (
          <polyline
            key={s.model}
            fill="none"
            strokeWidth={2}
            stroke={PALETTE[index % PALETTE.length]}
            points={s.loss
              .map((v, i) => (v === null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`))
              .filter((p): p is string => p !== null)
              .join(" ")}
          />
        ))}
      </svg>
      <figcaption className="agg-legend">
        {series.map((s, index) => (
          <span key={s.model} style={{ color: PALETTE[index % PALETTE.length] }}>
            {`■ ${s.model} (lower is better)`}
          </span>
        ))}
      </figcaption>
    </figure>
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
          {latest.models.length > 0 && (
            <>
              <h3>Models, on the same test points</h3>
              <table className="agg-estimate-stages" aria-label="Models">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Scored</th>
                    <th>Unknown</th>
                    <th>Open tasks</th>
                    <th>Coverage</th>
                    <th>Pinball loss</th>
                    <th>Median error</th>
                    <th>Fitted</th>
                  </tr>
                </thead>
                <tbody>
                  {latest.models.map((m) => {
                    const fit = accuracy.fits.find((f) => f.model === m.model);
                    return (
                      <tr key={m.model}>
                        <td>{m.model}</td>
                        <td>{m.overall.n - m.overall.unknown - m.overall.censored}</td>
                        <td>{m.overall.unknown}</td>
                        <td>{m.overall.censored}</td>
                        <td>{percent(m.overall.coverage)}</td>
                        <td>{m.overall.loss === null ? "–" : formatWorking(m.overall.loss, day)}</td>
                        <td>{m.overall.median_abs_error_seconds === null ? "–" : formatWorking(m.overall.median_abs_error_seconds, day)}</td>
                        <td>{fit ? `${formatLocalInstant(Date.parse(fit.fitted_at), timeZone)} (${fit.trained_on} rows)` : m.model === "baseline" ? "on use" : "–"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <ModelLines accuracy={accuracy} />
              <h3>Champion per stage</h3>
              <table className="agg-estimate-stages" aria-label="Champions">
                <thead>
                  <tr>
                    <th>Stage</th>
                    <th>Champion</th>
                    <th>Margin over baseline</th>
                    <th>Test points</th>
                    <th>Loss by model</th>
                  </tr>
                </thead>
                <tbody>
                  {latest.champions
                    .filter((c) => c.stage === "all" || c.points > 0)
                    .map((c) => (
                    <tr key={c.stage}>
                      <td>{c.stage.replace(/_/g, " ")}</td>
                      <td>{c.model}</td>
                      <td>{c.model === "baseline" ? "–" : percent(c.margin)}</td>
                      <td>{c.points}</td>
                      <td>{c.evidence.map((e) => `${e.model} ${e.loss === null ? "–" : formatWorking(e.loss, day)}`).join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {accuracy.champion_changes.map((c) => (
                <p key={`${c.stage}:${c.at}`} className="agg-description">
                  {`${formatLocalInstant(Date.parse(c.at), timeZone)}: the champion for ${c.stage.replace(/_/g, " ")} changed from ${c.previous} to ${c.model}.`}
                </p>
              ))}
            </>
          )}
          {latest.ablation.length > 0 && (
            <>
              <h3>{`Which features carry signal (removed from ${latest.ablation_model})`}</h3>
              <table className="agg-estimate-stages" aria-label="Ablation">
                <thead>
                  <tr>
                    <th>Feature</th>
                    <th>Loss change when removed</th>
                    <th>Test points</th>
                  </tr>
                </thead>
                <tbody>
                  {[...latest.ablation]
                    .sort((a, b) => (b.delta ?? -Infinity) - (a.delta ?? -Infinity))
                    .map((a) => (
                      <tr key={a.feature}>
                        <td>{a.feature}</td>
                        <td>{a.delta === null ? "–" : `${a.delta > 0 ? "+" : ""}${formatWorking(Math.abs(a.delta), day)}${a.delta < 0 ? " (better without)" : a.delta > 0 ? " (signal)" : ""}`}</td>
                        <td>{a.points}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </>
          )}
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
