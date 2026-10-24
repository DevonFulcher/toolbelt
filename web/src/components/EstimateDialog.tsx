// The estimate popup of a card: every model's answer for the task side by side
// with the champion marked, each model's recent accuracy and what it learned,
// the typical path, the wait for a parent, and the most similar past tasks.
import { useEffect, useState } from "react";
import { fetchEstimateBreakdown } from "../api/client";
import type { EstimateBreakdown, ModelForecast } from "../api/generated";
import { formatAge } from "../logic/format";
import { formatWorking, percent } from "../logic/finishEstimate";
import { browserTimeZone, formatLocalInstant } from "../logic/localTime";
import { Dialog } from "./Dialog";

const DAY_SECONDS = 8 * 3600;

function until(instant: string | null, now: number): string {
  return instant === null ? "–" : `~${formatAge(Math.max(0, (Date.parse(instant) - now) / 1000))}`;
}

type Detail = Record<string, unknown>;

/** What the model learned that matters for this answer, as short lines. */
export function learned(forecast: ModelForecast): string[] {
  const d = forecast.details as Detail;
  const lines: string[] = [];
  const list = (key: string): Detail[] => (Array.isArray(d[key]) ? (d[key] as Detail[]) : []);
  if (forecast.model === "baseline") {
    lines.push(`level ${String(d.level ?? forecast.level)}, ${String(d.n ?? forecast.n)} samples`);
    for (const f of list("factors")) {
      lines.push(`${String(f.dim)} ${String(f.value)}: ×${Number(f.shrunk).toFixed(2)} (${String(f.n)} samples)`);
    }
  } else if (forecast.model === "regression") {
    for (const c of list("contributions").slice(0, 4)) {
      lines.push(`${String(c.feature)}: ${Number(c.contribution) >= 0 ? "+" : ""}${Number(c.contribution).toFixed(2)} log-seconds`);
    }
  } else if (forecast.model === "hazard") {
    for (const c of list("coefficients").slice(0, 3)) lines.push(`${String(c.feature)}: coefficient ${Number(c.coefficient).toFixed(2)}`);
    const hazard = list("hazard").filter((h) => Number(h.from_hours) <= 40);
    if (hazard.length > 0) {
      lines.push(`hazard by hour bin: ${hazard.map((h) => `${String(h.from_hours)}h ${Number(h.hazard).toFixed(2)}`).join(", ")}`);
    }
  } else {
    for (const c of list("importances").slice(0, 4)) lines.push(`${String(c.feature)}: importance ${Number(c.importance).toFixed(2)}`);
  }
  return lines;
}

function Body({ data, now, timeZone }: { data: EstimateBreakdown; now: number; timeZone: string }) {
  return (
    <div className="estimate-dialog">
      <p>
        <strong>{`${data.champion} answers the card`}</strong>
        {`: ${data.champion_reason}.`}
      </p>
      <table className="estimate-models" aria-label="Models">
        <thead>
          <tr>
            <th>Model</th>
            <th>p50</th>
            <th>p80</th>
            <th>Coverage</th>
            <th>Pinball loss</th>
            <th>Median error</th>
            <th>Points</th>
            <th>Fitted on</th>
          </tr>
        </thead>
        <tbody>
          {data.models.map((m) => {
            const a = data.accuracy.find((row) => row.model === m.model);
            return (
              <tr key={m.model} className={m.champion ? "champion" : undefined} title={m.explanation}>
                <td>{m.champion ? `★ ${m.model}` : m.model}</td>
                <td title={m.finish_p50 ? formatLocalInstant(Date.parse(m.finish_p50), timeZone) : undefined}>{m.known ? until(m.finish_p50, now) : "unknown"}</td>
                <td title={m.finish_p80 ? formatLocalInstant(Date.parse(m.finish_p80), timeZone) : undefined}>{m.known ? until(m.finish_p80, now) : "unknown"}</td>
                <td>{percent(a?.coverage ?? null)}</td>
                <td>
                  {a?.loss == null ? "–" : formatWorking(a.loss, DAY_SECONDS)}
                  {a?.loss_vs_baseline != null && m.model !== "baseline" ? ` (${a.loss_vs_baseline.toFixed(2)}× baseline)` : ""}
                </td>
                <td>{a?.median_abs_error_seconds == null ? "–" : formatWorking(a.median_abs_error_seconds, DAY_SECONDS)}</td>
                <td>{a?.points ?? "–"}</td>
                <td>{a?.trained_on == null ? "refit on use" : `${a.trained_on} rows, ${a.fitted_at ? formatLocalInstant(Date.parse(a.fitted_at), timeZone) : ""}`}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="agg-description">{`Accuracy is on the latest backtest's points for the ${data.stage.replace(/_/g, " ")} stage (all stages where it has none).`}</p>
      <h3>What each model learned</h3>
      <ul className="estimate-learned">
        {data.models.map((m) => (
          <li key={m.model}>
            <strong>{m.model}</strong>
            {`: ${m.explanation}`}
            <ul>
              {learned(m).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
      <h3>Stage path</h3>
      <ol className="estimate-path" aria-label="Stage path">
        <li>{`${data.stage.replace(/_/g, " ")} (now)`}</li>
        {data.parent && (
          <li className="estimate-parent">
            {data.parent.parent_known
              ? `wait for parent ${data.parent.task_key}: finishes ${until(data.parent.parent_p50, now)} (p50), up to ${until(data.parent.parent_p80, now)} (p80); combined with this task and what follows a parent landing: ${until(data.parent.combined_p50, now)} (p50), up to ${until(data.parent.combined_p80, now)} (p80)`
              : `wait for parent ${data.parent.task_key}: it has no estimate, so this is at least the task's own`}
          </li>
        )}
        {data.path.map((p) => (
          <li key={p.stage}>{`${p.stage.replace(/_/g, " ")}: ${formatWorking(p.p50_seconds, DAY_SECONDS)} (p50), up to ${formatWorking(p.p80_seconds, DAY_SECONDS)} (p80), in ${percent(p.share)} of similar tasks`}</li>
        ))}
      </ol>
      {data.factors.length > 0 && (
        <>
          <h3>Factors</h3>
          <ul>
            {data.factors.map((f) => (
              <li key={`${f.dim}:${f.value}`}>{`${f.dim} ${f.value}: ×${f.shrunk.toFixed(2)} (raw ×${f.raw.toFixed(2)}, ${f.n} samples)`}</li>
            ))}
          </ul>
        </>
      )}
      <h3>Most similar past tasks</h3>
      {data.similar.length === 0 ? (
        <p className="muted">None: not enough history for this stage.</p>
      ) : (
        <ul aria-label="Similar tasks">
          {data.similar.map((s) => (
            <li key={`${s.task_key}:${s.ended_at}`}>
              {`${s.task_key}${s.reviewer ? ` (${s.reviewer})` : ""}: ${formatWorking(s.seconds, DAY_SECONDS)} in ${s.stage.replace(/_/g, " ")}${s.tail_seconds === null ? "" : `, then ${formatWorking(s.tail_seconds, DAY_SECONDS)} to merge`}`}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function EstimateDialog({ id, branch, now, onClose }: { id: string; branch: string; now: number; onClose: () => void }) {
  const [data, setData] = useState<EstimateBreakdown | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    fetchEstimateBreakdown(id)
      .then((found) => live && setData(found))
      .catch((error: unknown) => live && setProblem(error instanceof Error ? error.message : String(error)));
    return () => {
      live = false;
    };
  }, [id]);
  return (
    <Dialog title={`Estimate: ${branch}`} onClose={onClose}>
      {problem ? <p className="error">{problem}</p> : data === null ? <p className="muted">Loading…</p> : <Body data={data} now={now} timeZone={browserTimeZone()} />}
    </Dialog>
  );
}
