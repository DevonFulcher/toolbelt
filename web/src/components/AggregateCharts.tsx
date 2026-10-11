// The aggregate view's charts: hand-written SVG (a handful of rects, paths
// and ticks do not need a charting library, and this way the colours are the
// page's own tone tokens, so light/dark and the card palette come for free).
// All arithmetic is in logic/aggregateChart.ts; these components draw it.
//
// Every chart has: labelled axes, a legend with the current/total values,
// an exact-values tooltip (pointer or arrow keys), an aria summary and a
// data table behind a toggle.
import {
  useCallback,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
} from "react";
import type { LeadStage, Tone } from "../api/generated";
import {
  bandPath,
  countTicks,
  durationAxis,
  formatBucketDate,
  formatBucketRange,
  formatDuration,
  labelIndexes,
  linearScale,
  logScale,
  niceMax,
  packStrip,
  rowOffset,
  stackShares,
} from "../logic/aggregateChart";
import "./aggregate.css";

const MARGIN = { left: 44, right: 12, top: 10, bottom: 30 };
const DEFAULT_WIDTH = 720;

/** The measured width of an element (a fixed default where nothing is
 * measured, e.g. jsdom). */
function useWidth(): [(el: HTMLElement | null) => void, number] {
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const observer = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: HTMLElement | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el || typeof ResizeObserver === "undefined") return;
    observer.current = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0].contentRect.width);
      if (next > 0) setWidth(next);
    });
    observer.current.observe(el);
  }, []);
  return [ref, width];
}

// ---- legend and swatches ---------------------------------------------------------

export interface SeriesStyle {
  key: string;
  label: string;
  tone: Tone;
  /** Hatched: a second series of a tone another one already uses. */
  hatch: boolean;
  legend: string;
}

/** Marks every series whose tone an earlier one already has as hatched, so
 * two bands of one palette tone stay apart (the palette stays the cards'). */
export function withHatches(series: Omit<SeriesStyle, "hatch">[]): SeriesStyle[] {
  const seen = new Set<Tone>();
  return series.map((s) => {
    const hatch = seen.has(s.tone);
    seen.add(s.tone);
    return { ...s, hatch };
  });
}

function Patterns({ id, series }: { id: string; series: SeriesStyle[] }) {
  const tones = [...new Set(series.filter((s) => s.hatch).map((s) => s.tone))];
  if (tones.length === 0) return null;
  return (
    <defs>
      {tones.map((tone) => (
        <pattern
          key={tone}
          id={`${id}-${tone}`}
          className={`tone-${tone}`}
          patternUnits="userSpaceOnUse"
          width="6"
          height="6"
          patternTransform="rotate(45)"
        >
          <rect width="6" height="6" fill="var(--tone)" fillOpacity="0.28" />
          <rect width="2.5" height="6" fill="var(--tone)" />
        </pattern>
      ))}
    </defs>
  );
}

const fillOf = (id: string, s: SeriesStyle): string => (s.hatch ? `url(#${id}-${s.tone})` : "var(--tone)");

function Swatch({ id, series }: { id: string; series: SeriesStyle }) {
  return (
    <svg className={`agg-swatch tone-${series.tone}`} width="14" height="14" aria-hidden="true">
      <rect width="14" height="14" rx="2" fill={fillOf(id, series)} />
    </svg>
  );
}

export function Legend({
  id,
  series,
  values,
}: {
  id: string;
  series: SeriesStyle[];
  /** Text after each label (a total, or the latest share). */
  values?: Record<string, string>;
}) {
  return (
    <ul className="agg-legend">
      {series.map((s) => (
        <li key={s.key} title={s.legend}>
          <Swatch id={id} series={s} />
          <span>{s.label}</span>
          {values?.[s.key] !== undefined && <span className="agg-legend-value">{values[s.key]}</span>}
        </li>
      ))}
    </ul>
  );
}

// ---- a framed chart ------------------------------------------------------------------

export function ChartFrame({
  title,
  description,
  legend,
  children,
  table,
}: {
  title: string;
  description: ReactNode;
  legend?: ReactNode;
  children: ReactNode;
  /** Rendered (a `<table>`) when the data table is toggled on. */
  table: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <section className="agg-chart" aria-labelledby={`${id}-title`}>
      <header>
        <h2 id={`${id}-title`}>{title}</h2>
        <button
          type="button"
          className="agg-table-toggle"
          aria-expanded={open}
          aria-controls={`${id}-table`}
          onClick={() => setOpen(!open)}
        >
          {open ? "Hide data table" : "Show data table"}
        </button>
      </header>
      <p className="agg-description">{description}</p>
      {legend}
      {children}
      <div id={`${id}-table`} className="agg-table-wrap" hidden={!open}>
        {open && table}
      </div>
    </section>
  );
}

function EmptyChart({ children }: { children: ReactNode }) {
  return <div className="agg-empty">{children}</div>;
}

// ---- tooltip ---------------------------------------------------------------------------

interface Hover {
  index: number;
  /** Pixel x of the bucket's centre within the chart. */
  x: number;
}

function ChartTooltip({
  hover,
  width,
  children,
}: {
  hover: Hover | null;
  width: number;
  children: ReactNode;
}) {
  if (!hover) return null;
  const right = hover.x > width / 2;
  return (
    <div
      className="agg-tip"
      role="status"
      style={right ? { right: width - hover.x + 10 } : { left: hover.x + 10 }}
    >
      {children}
    </div>
  );
}

// ---- throughput ----------------------------------------------------------------------------

export interface ThroughputSeries {
  opened: SeriesStyle;
  merged: SeriesStyle;
}

export function ThroughputChart({
  starts,
  opened,
  merged,
  closed,
  styles,
}: {
  starts: string[];
  opened: number[];
  merged: number[];
  closed: number[];
  styles: ThroughputSeries;
}) {
  const [ref, width] = useWidth();
  const id = useId();
  const [hover, setHover] = useState<Hover | null>(null);
  const height = 220;
  const plotW = Math.max(width - MARGIN.left - MARGIN.right, 50);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const max = Math.max(1, ...opened, ...merged);
  const ticks = countTicks(max);
  const top = niceMax(ticks[ticks.length - 1]);
  const y = linearScale([0, top], [MARGIN.top + plotH, MARGIN.top]);
  const slot = plotW / Math.max(starts.length, 1);
  const barW = Math.max(Math.min(slot * 0.4, 22), 2);
  const centre = (i: number): number => MARGIN.left + slot * (i + 0.5);
  const totalOpened = opened.reduce((a, b) => a + b, 0);
  const totalMerged = merged.reduce((a, b) => a + b, 0);
  const labelled = slot >= 14;

  const move = (event: PointerEvent<SVGRectElement>): void => {
    const box = event.currentTarget.getBoundingClientRect();
    const index = Math.min(starts.length - 1, Math.max(0, Math.floor((event.clientX - box.left) / slot)));
    setHover({ index, x: centre(index) });
  };
  const key = (event: KeyboardEvent): void => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (event.key === "Escape") setHover(null);
    if (!step) return;
    event.preventDefault();
    const next = Math.min(starts.length - 1, Math.max(0, (hover?.index ?? (step > 0 ? -1 : starts.length)) + step));
    setHover({ index: next, x: centre(next) });
  };

  const summary = `${totalOpened} PRs opened and ${totalMerged} merged over ${starts.length} weeks, from the week of ${formatBucketDate(starts[0])}.`;
  const both = [styles.opened, styles.merged];
  return (
    <ChartFrame
      title="Throughput"
      description="PRs opened and merged per week (weeks start on Monday, UTC; the last is the current week so far)."
      legend={
        <Legend
          id={id}
          series={both}
          values={{ opened: `${totalOpened} total`, merged: `${totalMerged} total` }}
        />
      }
      table={
        <table className="agg-table">
          <caption>PRs per week</caption>
          <thead>
            <tr>
              <th scope="col">Week of</th>
              <th scope="col">Opened</th>
              <th scope="col">Merged</th>
              <th scope="col">Closed unmerged</th>
            </tr>
          </thead>
          <tbody>
            {starts.map((start, i) => (
              <tr key={start}>
                <th scope="row">{formatBucketDate(start)}</th>
                <td>{opened[i]}</td>
                <td>{merged[i]}</td>
                <td>{closed[i]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      <div className="agg-plot" ref={ref}>
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={summary}
          tabIndex={0}
          onKeyDown={key}
          onBlur={() => setHover(null)}
        >
          <Patterns id={id} series={both} />
          {ticks.map((t) => (
            <g key={t}>
              <line className="agg-grid" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={y(t)} y2={y(t)} />
              <text className="agg-tick" x={MARGIN.left - 6} y={y(t)} textAnchor="end" dominantBaseline="middle">
                {t}
              </text>
            </g>
          ))}
          {labelIndexes(starts.length, Math.max(2, Math.floor(plotW / 70))).map((i) => (
            <text key={i} className="agg-tick" x={centre(i)} y={height - 10} textAnchor="middle">
              {formatBucketDate(starts[i])}
            </text>
          ))}
          <text className="agg-axis-title" transform={`translate(11 ${MARGIN.top + plotH / 2}) rotate(-90)`} textAnchor="middle">
            PRs per week
          </text>
          {starts.map((start, i) => (
            <g key={start} className={hover?.index === i ? "agg-col agg-col-active" : "agg-col"}>
              {([
                [styles.opened, opened[i], -barW],
                [styles.merged, merged[i], 0],
              ] as const).map(([series, value, dx]) => (
                <g key={series.key} className={`tone-${series.tone}`}>
                  <rect
                    x={centre(i) + dx}
                    y={y(value)}
                    width={barW}
                    height={Math.max(y(0) - y(value), 0)}
                    fill={fillOf(id, series)}
                  />
                </g>
              ))}
              {labelled && merged[i] > 0 && (
                <text className="agg-value" x={centre(i) + barW / 2} y={y(merged[i]) - 3} textAnchor="middle">
                  {merged[i]}
                </text>
              )}
            </g>
          ))}
          <line className="agg-axis" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={y(0)} y2={y(0)} />
          <rect
            className="agg-hit"
            x={MARGIN.left}
            y={MARGIN.top}
            width={plotW}
            height={plotH}
            onPointerMove={move}
            onPointerLeave={() => setHover(null)}
          />
        </svg>
        <ChartTooltip hover={hover} width={width}>
          {hover && (
            <>
              <strong>{`Week of ${formatBucketDate(starts[hover.index])}`}</strong>
              <div>{`${opened[hover.index]} opened`}</div>
              <div>{`${merged[hover.index]} merged`}</div>
              <div>{`${closed[hover.index]} closed unmerged`}</div>
            </>
          )}
        </ChartTooltip>
      </div>
    </ChartFrame>
  );
}

// ---- lead time --------------------------------------------------------------------------------

/** Width of the lead-time label column and the width below which the labels
 * sit above their strips (the stylesheet's `.agg-lead` uses the same). */
const LEAD_LABEL = 280;
const LEAD_NARROW = 560;
const START_STAGES = new Set(["start_to_open", "start_to_merge"]);

export function LeadTimeChart({
  stages,
  mergedPrs,
  startsSince,
  tone,
}: {
  stages: LeadStage[];
  /** PRs merged in the window (what every stage is out of). */
  mergedPrs: number;
  /** When task starts began to be recorded (ISO). */
  startsSince: string;
  tone: Tone;
}) {
  const [ref, width] = useWidth();
  const all = stages.flatMap((s) => s.values_s);
  const axis = durationAxis(all);
  // Label column beside the strips, or above them on a narrow chart.
  const narrow = width < LEAD_NARROW;
  const plotW = Math.max(width - (narrow ? 0 : LEAD_LABEL) - 8, 60);
  const x = logScale(axis.domain, [6, plotW - 6]);
  const dot = 6;
  const aria = stages
    .map((s) =>
      s.n > 0
        ? `${s.label}: median ${formatDuration(s.median_s ?? 0)} over ${s.n} PRs`
        : `${s.label}: no data`,
    )
    .join("; ");
  return (
    <ChartFrame
      title="Lead time"
      description={
        <>
          How long each step took, for the {mergedPrs} PR{mergedPrs === 1 ? "" : "s"} merged in this window. Each dot is
          a PR; the heavy mark is the median, the lighter ones the 75th and 90th percentiles. Log scale.
        </>
      }
      table={
        <table className="agg-table">
          <caption>Lead time by step</caption>
          <thead>
            <tr>
              <th scope="col">Step</th>
              <th scope="col">PRs</th>
              <th scope="col">Unknown</th>
              <th scope="col">Median</th>
              <th scope="col">p75</th>
              <th scope="col">p90</th>
            </tr>
          </thead>
          <tbody>
            {stages.map((s) => (
              <tr key={s.key}>
                <th scope="row">{s.label}</th>
                <td>{s.n}</td>
                <td>{s.unknown}</td>
                <td>{s.median_s === null ? "-" : formatDuration(s.median_s)}</td>
                <td>{s.p75_s === null ? "-" : formatDuration(s.p75_s)}</td>
                <td>{s.p90_s === null ? "-" : formatDuration(s.p90_s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      <div
        className={`agg-lead tone-${tone}${narrow ? " agg-lead-narrow" : ""}`}
        role="group"
        aria-label={aria}
        ref={ref}
      >
        {stages.map((stage) => (
          <LeadRow
            key={stage.key}
            stage={stage}
            x={x}
            width={plotW}
            dot={dot}
            reason={
              START_STAGES.has(stage.key)
                ? `No task start was recorded for these PRs (starts are recorded since ${formatBucketDate(startsSince)}).`
                : "No PR merged in this window has this step."
            }
          />
        ))}
        <div className="agg-lead-row agg-lead-axis">
          <div className="agg-lead-label" />
          <div className="agg-lead-strip">
            <svg width={plotW} height={30} aria-hidden="true">
              {axis.ticks.map((t) => (
                <g key={t}>
                  <line className="agg-grid" x1={x(t)} x2={x(t)} y1={0} y2={5} />
                  <text className="agg-tick" x={x(t)} y={18} textAnchor="middle">
                    {formatDuration(t)}
                  </text>
                </g>
              ))}
              <text className="agg-axis-title" x={plotW / 2} y={29} textAnchor="middle">
                time (log scale)
              </text>
            </svg>
          </div>
        </div>
      </div>
    </ChartFrame>
  );
}

function LeadRow({
  stage,
  x,
  width,
  dot,
  reason,
}: {
  stage: LeadStage;
  x: (seconds: number) => number;
  width: number;
  dot: number;
  reason: string;
}) {
  const xs = stage.values_s.map(x);
  const { rows, used } = packStrip(xs, dot, 5);
  const height = Math.max(used, 1) * dot + 14;
  const mid = height / 2;
  const stat = (label: string, value: number | null): string =>
    value === null ? "" : `${label} ${formatDuration(value)}`;
  return (
    <div className="agg-lead-row">
      <div className="agg-lead-label">
        <strong>{stage.label}</strong>
        {stage.n > 0 ? (
          <span className="agg-lead-stats">
            {[stat("median", stage.median_s), stat("p75", stage.p75_s), stat("p90", stage.p90_s)]
              .filter(Boolean)
              .join(" · ")}
            {` · n=${stage.n}`}
            {stage.unknown > 0 && ` (${stage.unknown} unknown)`}
          </span>
        ) : (
          <span className="agg-lead-stats">{`no data${stage.unknown > 0 ? ` (${stage.unknown} unknown)` : ""}`}</span>
        )}
      </div>
      <div className="agg-lead-strip">
        {stage.n > 0 ? (
          <svg width={width} height={height} aria-hidden="true">
            {stage.median_s !== null && (
              <>
                {[
                  [stage.p90_s, "agg-mark-light"],
                  [stage.p75_s, "agg-mark-light"],
                  [stage.median_s, "agg-mark-heavy"],
                ].map(([value, cls]) => (
                  <line
                    key={`${cls}${value}`}
                    className={cls as string}
                    x1={x(value as number)}
                    x2={x(value as number)}
                    y1={1}
                    y2={height - 1}
                  />
                ))}
              </>
            )}
            {stage.values_s.map((value, i) => (
              <circle
                key={i}
                className="agg-dot"
                cx={xs[i]}
                cy={mid + rowOffset(rows[i], dot)}
                r={dot / 2 - 0.5}
              >
                <title>{`${formatDuration(value)}`}</title>
              </circle>
            ))}
          </svg>
        ) : (
          <span className="agg-lead-empty">{reason}</span>
        )}
      </div>
    </div>
  );
}

// ---- stacked shares --------------------------------------------------------------------------------

export function ShareChart({
  title,
  description,
  series,
  shares,
  starts,
  inflight,
  granularity,
  empty,
}: {
  title: string;
  description: ReactNode;
  series: SeriesStyle[];
  /** Per series (bottom to top), the share of each bucket or null. */
  shares: (number | null)[][];
  starts: string[];
  inflight: number[];
  granularity: "day" | "week";
  /** Said when nothing was in flight in the whole window. */
  empty: ReactNode;
}) {
  const [ref, width] = useWidth();
  const id = useId();
  const [hover, setHover] = useState<Hover | null>(null);
  const height = 240;
  const plotW = Math.max(width - MARGIN.left - MARGIN.right, 50);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const count = starts.length;
  const slot = plotW / Math.max(count, 1);
  const centre = (i: number): number => MARGIN.left + slot * (i + 0.5);
  const y = linearScale([0, 1], [MARGIN.top + plotH, MARGIN.top]);
  const bands = stackShares(shares);
  const days = granularity === "day" ? 1 : 7;
  const hasData = shares.some((row) => row.some((v) => v !== null));
  const last = (() => {
    for (let i = count - 1; i >= 0; i--) if (shares.some((row) => row[i] !== null)) return i;
    return -1;
  })();
  const pct = (v: number | null): string => (v === null ? "-" : `${Math.round(v * 100)}%`);

  if (!hasData) {
    return (
      <ChartFrame title={title} description={description} table={null}>
        <EmptyChart>{empty}</EmptyChart>
      </ChartFrame>
    );
  }

  const move = (event: PointerEvent<SVGRectElement>): void => {
    const box = event.currentTarget.getBoundingClientRect();
    const index = Math.min(count - 1, Math.max(0, Math.floor((event.clientX - box.left) / slot)));
    setHover({ index, x: centre(index) });
  };
  const key = (event: KeyboardEvent): void => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (event.key === "Escape") setHover(null);
    if (!step) return;
    event.preventDefault();
    const next = Math.min(count - 1, Math.max(0, (hover?.index ?? (step > 0 ? -1 : count)) + step));
    setHover({ index: next, x: centre(next) });
  };

  const latest: Record<string, string> = {};
  series.forEach((s, i) => {
    latest[s.key] = last >= 0 ? pct(shares[i][last]) : "-";
  });
  const summary = `${title}, ${formatBucketDate(starts[0])} to ${formatBucketDate(starts[count - 1])}. Latest: ${series
    .map((s) => `${s.label} ${latest[s.key]}`)
    .join(", ")}.`;
  const ticks = [0, 0.25, 0.5, 0.75, 1];

  return (
    <ChartFrame
      title={title}
      description={description}
      legend={<Legend id={id} series={series} values={latest} />}
      table={
        <table className="agg-table">
          <caption>{`${title}, share of in-flight task time per ${granularity}`}</caption>
          <thead>
            <tr>
              <th scope="col">{granularity === "day" ? "Day" : "Week of"}</th>
              <th scope="col">In flight</th>
              {series.map((s) => (
                <th key={s.key} scope="col">
                  {s.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {starts.map((start, i) => (
              <tr key={start}>
                <th scope="row">{formatBucketDate(start)}</th>
                <td>{inflight[i].toFixed(1)}</td>
                {series.map((s, k) => (
                  <td key={s.key}>{pct(shares[k][i])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      <div className="agg-plot" ref={ref}>
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={summary}
          tabIndex={0}
          onKeyDown={key}
          onBlur={() => setHover(null)}
        >
          <Patterns id={id} series={series} />
          {ticks.map((t) => (
            <g key={t}>
              <line className="agg-grid" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={y(t)} y2={y(t)} />
              <text className="agg-tick" x={MARGIN.left - 6} y={y(t)} textAnchor="end" dominantBaseline="middle">
                {`${t * 100}%`}
              </text>
            </g>
          ))}
          {labelIndexes(count, Math.max(2, Math.floor(plotW / 70))).map((i) => (
            <text key={i} className="agg-tick" x={centre(i)} y={height - 10} textAnchor="middle">
              {formatBucketDate(starts[i])}
            </text>
          ))}
          <text className="agg-axis-title" transform={`translate(11 ${MARGIN.top + plotH / 2}) rotate(-90)`} textAnchor="middle">
            share of in-flight time
          </text>
          {series.map((s, i) => (
            <path
              key={s.key}
              className={`tone-${s.tone} agg-band`}
              d={bandPath(bands[i], centre, y, Math.min(slot, 24))}
              fill={fillOf(id, s)}
            />
          ))}
          {hover && (
            <line className="agg-cursor" x1={hover.x} x2={hover.x} y1={MARGIN.top} y2={MARGIN.top + plotH} />
          )}
          <rect
            className="agg-hit"
            x={MARGIN.left}
            y={MARGIN.top}
            width={plotW}
            height={plotH}
            onPointerMove={move}
            onPointerLeave={() => setHover(null)}
          />
        </svg>
        <ChartTooltip hover={hover} width={width}>
          {hover && (
            <>
              <strong>{formatBucketRange(starts[hover.index], days)}</strong>
              <div className="agg-tip-sub">
                {inflight[hover.index] > 0
                  ? `${inflight[hover.index].toFixed(1)} tasks in flight on average`
                  : "nothing in flight"}
              </div>
              {[...series].reverse().map((s) => {
                const k = series.indexOf(s);
                const v = shares[k][hover.index];
                return (
                  <div key={s.key} className="agg-tip-row">
                    <Swatch id={id} series={s} />
                    <span>{s.label}</span>
                    <span className="agg-tip-value">{pct(v)}</span>
                  </div>
                );
              })}
            </>
          )}
        </ChartTooltip>
      </div>
    </ChartFrame>
  );
}
