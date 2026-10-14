// The spend view: what Claude Code on this machine has cost, by window, by
// model and by task. Fetches `/api/spend` when opened, when the window
// changes, and every minute while the page is visible (the server reads the
// session transcripts about that often).
import { useEffect, useRef, useState } from "react";
import { fetchSpend } from "../api/client";
import type { Spend, SpendTokens } from "../api/generated";
import { TRIGGER_LABEL } from "./AgentsView";
import { formatAbsolute } from "../logic/format";
import { formatTokens, formatUsd } from "../logic/spendFormat";
import type { Counts } from "../store/selectors";
import { useApp } from "../store/StoreContext";
import { FilterBar } from "./FilterBar";
import "./spend.css";

/** How often the page asks again while it is visible. */
export const REFRESH_MS = 60_000;

type Load =
  | { days: number; state: "ready"; data: Spend }
  | { days: number; state: "error"; message: string };

/** The spend for `days`, or `null` while it is first being fetched. */
export function useSpend(days: number, fetchImpl: typeof fetch = fetch): Load | null {
  const [load, setLoad] = useState<Load | null>(null);
  const latest = useRef(0);
  useEffect(() => {
    let alive = true;
    const run = (): void => {
      const id = ++latest.current;
      fetchSpend(days, fetchImpl)
        .then((data) => {
          if (alive && id === latest.current) setLoad({ days, state: "ready", data });
        })
        .catch((error: unknown) => {
          if (alive && id === latest.current) {
            setLoad({ days, state: "error", message: error instanceof Error ? error.message : String(error) });
          }
        });
    };
    run();
    const timer = window.setInterval(() => {
      if (!document.hidden) run();
    }, REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [days, fetchImpl]);
  return load;
}

/** Runs cost cents or less: four digits under a dollar. */
const formatRunCost = (usd: number): string => (usd > 0 && usd < 1 ? `$${usd.toFixed(4)}` : formatUsd(usd));

const totalTokens = (tokens: SpendTokens): number =>
  tokens.input + tokens.output + tokens.cache_read + tokens.cache_write_5m + tokens.cache_write_1h;

export function SpendPage({ counts }: { counts: Counts }) {
  const days = useApp((s) => s.view.days);
  const load = useSpend(days);
  return (
    <>
      <FilterBar counts={counts} />
      {load === null || load.days !== days ? (
        <div className="empty">{`Loading the last ${days === 1 ? "day" : `${days} days`} of spend…`}</div>
      ) : load.state === "error" ? (
        <div className="empty error">{`Could not load the spend: ${load.message}`}</div>
      ) : (
        <SpendBody data={load.data} />
      )}
    </>
  );
}

function SpendBody({ data }: { data: Spend }) {
  const windowLabel = data.breakdown_days === 1 ? "day" : `${data.breakdown_days} days`;
  const unpriced = data.unpriced_models.reduce((total, u) => total + u.calls, 0);
  return (
    <section className="spend" aria-label="Spend">
      <p className="spend-summary">
        {data.ingested_at === null
          ? "Reading the session transcripts for the first time…"
          : `Claude Code on this machine, at list prices. Transcripts last read ${formatAbsolute(Date.parse(data.ingested_at))}.`}
      </p>
      <div className="spend-notices">
        {data.unpriced_models.length > 0 && (
          <div className="notice" role="alert">
            {`No price for ${data.unpriced_models.map((u) => `${u.model} (${u.calls} calls)`).join(", ")}: ${unpriced === 1 ? "that call is" : "those calls are"} counted in tokens but not in cost, so the cost below is too low. A newer price table is needed: run "uv lock --upgrade-package genai-prices" in the toolbelt checkout, then reinstall.`}
          </div>
        )}
        {data.claims_since === null ? (
          <div className="notice">No claims have been recorded yet, so every call below is unclaimed.</div>
        ) : (
          <div className="notice">
            {`Task attribution uses claims recorded since ${formatAbsolute(Date.parse(data.claims_since))}; earlier spend is unclaimed.`}
          </div>
        )}
      </div>
      <div className="spend-tiles">
        {data.windows.map((window) => (
          <div className="spend-tile" key={window.label}>
            <span className="spend-tile-label">{window.label}</span>
            <strong className="spend-tile-cost">{formatUsd(window.cost_usd)}</strong>
            <span className="spend-tile-detail">
              {`${window.calls.toLocaleString("en-US")} calls · ${formatTokens(totalTokens(window.tokens))} tokens`}
            </span>
          </div>
        ))}
      </div>
      <section className="spend-table" aria-label="By model">
        <h2>{`By model, last ${windowLabel}`}</h2>
        {data.by_model.length === 0 ? (
          <p className="spend-empty">No calls in this window.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Model</th>
                <th className="num">Calls</th>
                <th className="num">Tokens</th>
                <th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {data.by_model.map((model) => (
                <tr key={model.model}>
                  <td>
                    {model.model}
                    {model.unpriced_calls > 0 && <span className="spend-flag"> no price</span>}
                  </td>
                  <td className="num">{model.calls.toLocaleString("en-US")}</td>
                  <td className="num">{formatTokens(totalTokens(model.tokens))}</td>
                  <td className="num">{model.unpriced_calls === model.calls ? "–" : formatUsd(model.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="spend-table" aria-label="By task">
        <h2>{`By task, last ${windowLabel}`}</h2>
        <p className="spend-description">
          A call belongs to the task a claim of its session covered when it was made (split evenly when two tasks were
          claimed at once). Calls no claim covered are listed as unclaimed.
        </p>
        {data.by_task.length === 0 ? (
          <p className="spend-empty">No calls in this window.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Task</th>
                <th className="num">Calls</th>
                <th className="num">Tokens</th>
                <th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {data.by_task.map((task) => (
                <tr key={task.task_id ?? "unclaimed"} className={task.task_id === null ? "spend-unclaimed" : undefined}>
                  <td>
                    {task.task_id === null
                      ? "Unclaimed"
                      : task.branch === null
                        ? `Task ${task.task_id.slice(0, 8)} (no longer tracked)`
                        : `${task.repo}: ${task.branch}`}
                  </td>
                  <td className="num">{Math.round(task.calls).toLocaleString("en-US")}</td>
                  <td className="num">{formatTokens(task.tokens)}</td>
                  <td className="num">{formatUsd(task.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="spend-table" aria-label="Autonomous agents">
        <h2>Autonomous agents</h2>
        <p className="spend-description">
          What toolbelt's autonomous agents spent, exactly: each run records the cost the SDK reported. Their sessions are
          not in the figures above.
        </p>
        <div className="spend-tiles">
          {data.autonomous.windows.map((window) => (
            <div className="spend-tile" key={window.label}>
              <span className="spend-tile-label">{window.label}</span>
              <strong className="spend-tile-cost">{formatRunCost(window.cost_usd)}</strong>
              <span className="spend-tile-detail">{`${window.runs} run${window.runs === 1 ? "" : "s"} · ${formatTokens(totalTokens(window.tokens))} tokens`}</span>
            </div>
          ))}
        </div>
        {data.autonomous.by_trigger.length === 0 ? (
          <p className="spend-empty">{`No autonomous run in the last ${windowLabel}.`}</p>
        ) : (
          <>
            <table aria-label="Autonomous spend by what the run was for">
              <thead>
                <tr>
                  <th>{`What it was for, last ${windowLabel}`}</th>
                  <th className="num">Runs</th>
                  <th className="num">Cost</th>
                </tr>
              </thead>
              <tbody>
                {data.autonomous.by_trigger.map((row) => (
                  <tr key={row.trigger}>
                    <td>{TRIGGER_LABEL[row.trigger]}</td>
                    <td className="num">{row.runs}</td>
                    <td className="num">{formatRunCost(row.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <table aria-label="Autonomous spend by task">
              <thead>
                <tr>
                  <th>{`Task, last ${windowLabel}`}</th>
                  <th className="num">Runs</th>
                  <th className="num">Cost</th>
                </tr>
              </thead>
              <tbody>
                {data.autonomous.by_task.map((row) => (
                  <tr key={row.task_id}>
                    <td>{row.repo === null ? row.branch : `${row.repo}: ${row.branch}`}</td>
                    <td className="num">{row.runs}</td>
                    <td className="num">{formatRunCost(row.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>
      <details className="spend-notes">
        <summary>What these numbers are</summary>
        <dl>
          <div>
            <dt>This machine only</dt>
            <dd>
              They come from this machine's Claude Code session transcripts, so other machines, claude.ai and API use
              are not in them.
            </dd>
          </div>
          <div>
            <dt>List prices</dt>
            <dd>
              Each call is priced from the genai-prices table, which matches what Claude Code itself reports for a
              call. It is not an invoice: an enterprise contract can differ.
            </dd>
          </div>
          <div>
            <dt>Cost excludes unpriced calls</dt>
            <dd>A model the table does not know yet has its tokens counted and its cost left out, never priced at zero silently.</dd>
          </div>
        </dl>
      </details>
    </section>
  );
}
