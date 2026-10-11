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
import type { Tone } from "../api/generated";
import {
  bandPath,
  countTicks,
  dayLabels,
  durationAxis,
  formatBucketDate,
  formatDuration,
  labelIndexes,
  linePath,
  linearScale,
  medianPositive,
  nearestIndex,
  niceMax,
  rangePath,
  stackShares,
  weekendSpans,
} from "../logic/aggregateChart";
import { TOTAL_STAGE, type LeadTimeSeries, type StageKey } from "../logic/leadTime";
import {
  addLocalDays,
  bucketIndex,
  formatLocalDate,
  formatLocalDay,
  formatLocalInstant,
} from "../logic/localTime";
import type { Throughput } from "../logic/throughput";
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
    if (!el) return;
    // Start from the real width, not the default: no frame at the wrong size.
    const initial = Math.floor(el.getBoundingClientRect().width);
    if (initial > 0) setWidth(initial);
    if (typeof ResizeObserver === "undefined") return;
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

// ---- weekends ---------------------------------------------------------------------------------

/** The hatched pattern weekend bands are filled with (the card timeline's
 * weekend look: the muted tone, faintly). */
function WeekendPattern({ id }: { id: string }) {
  return (
    <defs>
      <pattern
        id={`${id}-weekend`}
        patternUnits="userSpaceOnUse"
        width="5"
        height="5"
        patternTransform="rotate(135)"
      >
        <rect width="2" height="5" className="agg-weekend-line" />
      </pattern>
    </defs>
  );
}

/** Weekend bands over a plot of `[from, to]` (ms) drawn between `top` and `bottom`. */
function WeekendBands({
  id,
  from,
  to,
  timeZone,
  x,
  top,
  bottom,
}: {
  id: string;
  from: number;
  to: number;
  timeZone: string;
  x: (ms: number) => number;
  top: number;
  bottom: number;
}) {
  return (
    <g aria-hidden="true">
      {weekendSpans(from, to, timeZone).map(([start, end]) => (
        <rect
          key={start}
          className="agg-weekend"
          x={x(start)}
          y={top}
          width={Math.max(x(end) - x(start), 0)}
          height={bottom - top}
          fill={`url(#${id}-weekend)`}
        />
      ))}
    </g>
  );
}

// ---- throughput ----------------------------------------------------------------------------

export interface ThroughputSeries {
  opened: SeriesStyle;
  merged: SeriesStyle;
}

export function ThroughputChart({
  data,
  timeZone,
  styles,
}: {
  data: Throughput;
  timeZone: string;
  styles: ThroughputSeries;
}) {
  const [ref, width] = useWidth();
  const id = useId();
  const [hover, setHover] = useState<Hover | null>(null);
  const { buckets, opened, merged, closed, granularity } = data;
  const daily = granularity === "day";
  const height = 220;
  const plotW = Math.max(width - MARGIN.left - MARGIN.right, 50);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const max = Math.max(1, ...opened, ...merged);
  const ticks = countTicks(max);
  const top = niceMax(ticks[ticks.length - 1]);
  const y = linearScale([0, top], [MARGIN.top + plotH, MARGIN.top]);
  const slot = plotW / Math.max(buckets.length, 1);
  const barW = Math.max(Math.min(slot * 0.4, 22), 1);
  const centre = (i: number): number => MARGIN.left + slot * (i + 0.5);
  const totalOpened = opened.reduce((a, b) => a + b, 0);
  const totalMerged = merged.reduce((a, b) => a + b, 0);
  const labelled = slot >= 14;
  const first = buckets[0].start;
  const lastEnd = buckets[buckets.length - 1].end;
  const bucketX = (ms: number): number => MARGIN.left + ((ms - first) / Math.max(lastEnd - first, 1)) * plotW;
  const dates = daily
    ? dayLabels(buckets, timeZone, slot)
    : buckets.map((b, i) =>
        labelIndexes(buckets.length, Math.max(2, Math.floor(plotW / 70))).includes(i)
          ? formatLocalDate(b.start, timeZone)
          : null,
      );
  const title = (i: number): string =>
    daily ? formatLocalDay(buckets[i].start, timeZone) : `Week of ${formatLocalDay(buckets[i].start, timeZone)}`;

  const move = (event: PointerEvent<SVGRectElement>): void => {
    const box = event.currentTarget.getBoundingClientRect();
    const index = Math.min(buckets.length - 1, Math.max(0, Math.floor((event.clientX - box.left) / slot)));
    setHover({ index, x: centre(index) });
  };
  const key = (event: KeyboardEvent): void => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (event.key === "Escape") setHover(null);
    if (!step) return;
    event.preventDefault();
    const next = Math.min(
      buckets.length - 1,
      Math.max(0, (hover?.index ?? (step > 0 ? -1 : buckets.length)) + step),
    );
    setHover({ index: next, x: centre(next) });
  };

  const summary = `${totalOpened} PRs opened and ${totalMerged} merged over ${buckets.length} ${daily ? "days" : "weeks"}, from ${formatLocalDay(first, timeZone)}.`;
  const both = [styles.opened, styles.merged];
  return (
    <ChartFrame
      title="Throughput"
      description={
        daily
          ? `PRs opened and merged per day in your time zone (${timeZone}); Saturdays and Sundays are hatched. The last day is today so far.`
          : `PRs opened and merged per week in your time zone (${timeZone}); weeks start on Monday and each spans a weekend, so no weekends are marked. The last is the current week so far.`
      }
      legend={
        <Legend
          id={id}
          series={both}
          values={{ opened: `${totalOpened} total`, merged: `${totalMerged} total` }}
        />
      }
      table={
        <table className="agg-table">
          <caption>{`PRs per ${daily ? "day" : "week"} (${timeZone})`}</caption>
          <thead>
            <tr>
              <th scope="col">{daily ? "Day" : "Week of"}</th>
              {daily && <th scope="col">Weekend</th>}
              <th scope="col">Opened</th>
              <th scope="col">Merged</th>
              <th scope="col">Closed unmerged</th>
            </tr>
          </thead>
          <tbody>
            {buckets.map((bucket, i) => (
              <tr key={bucket.start}>
                <th scope="row">{formatLocalDay(bucket.start, timeZone)}</th>
                {daily && <td>{bucket.weekend ? "yes" : ""}</td>}
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
          <WeekendPattern id={id} />
          {daily && (
            <WeekendBands
              id={id}
              from={first}
              to={lastEnd}
              timeZone={timeZone}
              x={bucketX}
              top={MARGIN.top}
              bottom={MARGIN.top + plotH}
            />
          )}
          {ticks.map((t) => (
            <g key={t}>
              <line className="agg-grid" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={y(t)} y2={y(t)} />
              <text className="agg-tick" x={MARGIN.left - 6} y={y(t)} textAnchor="end" dominantBaseline="middle">
                {t}
              </text>
            </g>
          ))}
          {dates.map((label, i) =>
            label === null ? null : (
              <text key={i} className="agg-tick" x={centre(i)} y={height - 10} textAnchor="middle">
                {label}
              </text>
            ),
          )}
          <text className="agg-axis-title" transform={`translate(11 ${MARGIN.top + plotH / 2}) rotate(-90)`} textAnchor="middle">
            {`PRs per ${daily ? "day" : "week"}`}
          </text>
          {buckets.map((bucket, i) => (
            <g key={bucket.start} className={hover?.index === i ? "agg-col agg-col-active" : "agg-col"}>
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
              <strong>{title(hover.index)}</strong>
              {buckets[hover.index].weekend && <div className="agg-tip-sub">weekend</div>}
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

// ---- lead time over time ------------------------------------------------------------------------

export interface LeadStageStyle extends SeriesStyle {
  key: StageKey;
}

export function LeadTimeChart({
  series,
  styles,
  timeZone,
  windowStart,
  now,
  modeLabel,
  excludeWeekends,
  onModeChange,
  startsSince,
}: {
  series: LeadTimeSeries;
  styles: LeadStageStyle[];
  timeZone: string;
  windowStart: number;
  now: number;
  /** "All time" or "Excluding weekends": said in the title, aria text and table. */
  modeLabel: string;
  excludeWeekends: boolean;
  onModeChange: (excludeWeekends: boolean) => void;
  /** When task starts began to be recorded (ISO), for the empty-stage reason. */
  startsSince: string;
}) {
  const [ref, width] = useWidth();
  const id = useId();
  const [hover, setHover] = useState<
    | { type: "bucket"; index: number; x: number }
    | { type: "point"; stage: StageKey; index: number; x: number }
    | null
  >(null);
  const [hidden, setHidden] = useState<ReadonlySet<StageKey>>(new Set());
  const [band, setBand] = useState(false);
  const visible = series.stages.filter((s) => !hidden.has(s.key));
  const height = 260;
  const plotW = Math.max(width - MARGIN.left - MARGIN.right, 50);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const total = series.stages.find((s) => s.key === TOTAL_STAGE);
  const bandShown = band && total !== undefined && !hidden.has(TOTAL_STAGE);
  const values = [
    ...visible.flatMap((s) => s.points.map((p) => p.seconds)),
    ...(bandShown && total ? total.buckets.map((b) => b.p90 ?? 0) : []),
  ];
  const axis = durationAxis(Math.max(0, ...values));
  const y = linearScale([0, axis.top], [MARGIN.top + plotH, MARGIN.top]);
  const span = Math.max(now - windowStart, 1);
  const xMs = (ms: number): number => MARGIN.left + (Math.min(Math.max(ms - windowStart, 0), span) / span) * plotW;
  const centres = series.buckets.map((b) => xMs((b.start + Math.min(b.end, now)) / 2));
  const bucketX = (i: number): number => centres[i];
  const style = (key: StageKey): LeadStageStyle => styles.find((x) => x.key === key) as LeadStageStyle;
  const label = (ms: number): string => formatLocalDate(ms, timeZone);
  const rangeLabel = (i: number): string => {
    const b = series.buckets[i];
    const last = addLocalDays(b.end, -1, timeZone);
    return b.end - b.start > 24 * 3_600_000 * 1.5 ? `${label(b.start)} - ${label(last)}` : label(b.start);
  };
  const dur = (seconds: number | null): string => (seconds === null ? "-" : formatDuration(seconds));
  const noneKnown = series.stages.every((s) => s.points.length === 0);

  const key = (event: KeyboardEvent): void => {
    if (event.key === "Escape") setHover(null);
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const count = series.buckets.length;
    const from = hover?.type === "bucket" ? hover.index : step > 0 ? -1 : count;
    const next = Math.min(count - 1, Math.max(0, from + step));
    setHover({ type: "bucket", index: next, x: centres[next] });
  };
  const move = (event: PointerEvent<SVGRectElement>): void => {
    const box = event.currentTarget.getBoundingClientRect();
    const ms = windowStart + ((event.clientX - box.left) / Math.max(box.width, 1)) * span;
    const index = Math.max(0, bucketIndex(series.buckets, ms));
    setHover({ type: "bucket", index, x: centres[index] });
  };
  const toggle = (stage: StageKey): void => {
    const next = new Set(hidden);
    if (next.has(stage)) next.delete(stage);
    else if (visible.length > 1) next.add(stage);
    setHidden(next);
  };

  const aria = `Lead time (${modeLabel}), ${label(windowStart)} to ${label(now)}, by merge date: ${series.stages
    .map((s) =>
      s.summary.n > 0
        ? `${s.label} median ${dur(s.summary.median)} over ${s.summary.n} PRs`
        : `${s.label} no data`,
    )
    .join("; ")}.`;

  return (
    <ChartFrame
      title={`Lead time over time (${modeLabel})`}
      description={
        <>
          Each PR is placed at the time it merged ({timeZone}), and each line is the median of a step over the PRs that
          merged in each {series.buckets.length > 0 && series.buckets[0].end - series.buckets[0].start < 4 * 86_400_000 ? "3-day" : "week"}{" "}
          bucket, in a {modeLabel.toLowerCase()} view. A bucket with no PR whose step is known is a gap. The dots are
          single PRs; hover one for its details.
        </>
      }
      legend={
        <div className="agg-lead-controls">
          <ul className="agg-legend">
            {styles.map((s) => (
              <li key={s.key} title={s.legend}>
                <button
                  type="button"
                  className="agg-legend-toggle"
                  aria-pressed={!hidden.has(s.key)}
                  onClick={() => toggle(s.key)}
                >
                  <Swatch id={id} series={s} />
                  <span>{s.label}</span>
                </button>
              </li>
            ))}
          </ul>
          <div className="agg-mode" role="group" aria-label="Lead time weekends">
            {(
              [
                [false, "All time"],
                [true, "Excluding weekends"],
              ] as const
            ).map(([value, text]) => (
              <button
                key={text}
                type="button"
                aria-pressed={excludeWeekends === value}
                onClick={() => onModeChange(value)}
              >
                {text}
              </button>
            ))}
            <label className="agg-check">
              <input type="checkbox" checked={band} onChange={(event) => setBand(event.target.checked)} />
              p75 to p90 band (total)
            </label>
          </div>
        </div>
      }
      table={
        <table className="agg-table">
          <caption>{`Lead time (${modeLabel}) per ${series.buckets.length ? "bucket" : "bucket"}: median (PRs known / unknown)`}</caption>
          <thead>
            <tr>
              <th scope="col">Merged in</th>
              {series.stages.map((s) => (
                <th key={s.key} scope="col">
                  {s.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {series.buckets.map((bucket, i) => (
              <tr key={bucket.start}>
                <th scope="row">{rangeLabel(i)}</th>
                {series.stages.map((s) => {
                  const b = s.buckets[i];
                  return (
                    <td key={s.key}>{b.n > 0 ? `${dur(b.median)} (${b.n} / ${b.unknown})` : `- (0 / ${b.unknown})`}</td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      {noneKnown ? (
        <EmptyChart>
          {series.merged === 0
            ? "No PR merged in this window."
            : `No merged PR has a known step (task starts are recorded since ${formatBucketDate(startsSince)}).`}
        </EmptyChart>
      ) : (
        <div className="agg-plot" ref={ref}>
          <svg
            width={width}
            height={height}
            role="img"
            aria-label={aria}
            tabIndex={0}
            onKeyDown={key}
            onBlur={() => setHover(null)}
          >
            <Patterns id={id} series={styles} />
            <WeekendPattern id={id} />
            <WeekendBands
              id={id}
              from={windowStart}
              to={now}
              timeZone={timeZone}
              x={xMs}
              top={MARGIN.top}
              bottom={MARGIN.top + plotH}
            />
            {axis.ticks.map((t) => (
              <g key={t.value}>
                <line className="agg-grid" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={y(t.value)} y2={y(t.value)} />
                <text className="agg-tick" x={MARGIN.left - 6} y={y(t.value)} textAnchor="end" dominantBaseline="middle">
                  {t.label}
                </text>
              </g>
            ))}
            {labelIndexes(series.buckets.length, Math.max(2, Math.floor(plotW / 70))).map((i) => (
              <text key={i} className="agg-tick" x={xMs(series.buckets[i].start)} y={height - 10} textAnchor="start">
                {label(series.buckets[i].start)}
              </text>
            ))}
            <text className="agg-axis-title" transform={`translate(11 ${MARGIN.top + plotH / 2}) rotate(-90)`} textAnchor="middle">
              {`time (${axis.unit === "m" ? "minutes" : axis.unit === "h" ? "hours" : "days"})`}
            </text>
            <rect
              className="agg-hit agg-hit-under"
              x={MARGIN.left}
              y={MARGIN.top}
              width={plotW}
              height={plotH}
              onPointerMove={move}
              onPointerLeave={() => setHover(null)}
            />
            {bandShown && total && (
              <path
                className="agg-range"
                d={rangePath(
                  total.buckets.map((b) => b.p75),
                  total.buckets.map((b) => b.p90),
                  bucketX,
                  y,
                  8,
                )}
              />
            )}
            {visible.map((stage) => {
              const st = style(stage.key);
              return (
                <g key={stage.key} className={`tone-${st.tone}`}>
                  {stage.points.map((point, i) => (
                    <circle
                      key={`${point.pr.repo}#${point.pr.number}-${i}`}
                      className="agg-dot"
                      cx={xMs(point.ms)}
                      cy={y(point.seconds)}
                      r={2.5}
                      onPointerEnter={() =>
                        setHover({ type: "point", stage: stage.key, index: i, x: xMs(point.ms) })
                      }
                      onPointerLeave={() => setHover(null)}
                    />
                  ))}
                </g>
              );
            })}
            {visible.map((stage) => {
              const st = style(stage.key);
              return (
                <path
                  key={stage.key}
                  className={`tone-${st.tone} agg-line${stage.key === TOTAL_STAGE ? " agg-line-total" : ""}`}
                  d={linePath(
                    stage.buckets.map((b) => b.median),
                    bucketX,
                    y,
                  )}
                  fill="none"
                  stroke="var(--tone)"
                  strokeDasharray={st.hatch ? "5 3" : undefined}
                />
              );
            })}
            {hover && (
              <line className="agg-cursor" x1={hover.x} x2={hover.x} y1={MARGIN.top} y2={MARGIN.top + plotH} />
            )}
          </svg>
          <ChartTooltip hover={hover} width={width}>
            {hover?.type === "bucket" && (
              <>
                <strong>{`Merged ${rangeLabel(hover.index)}`}</strong>
                {visible.map((stage) => {
                  const b = stage.buckets[hover.index];
                  return (
                    <div key={stage.key} className="agg-tip-row">
                      <Swatch id={id} series={style(stage.key)} />
                      <span>{style(stage.key).label}</span>
                      <span className="agg-tip-value">
                        {b.n > 0
                          ? `median ${dur(b.median)}, p75 ${dur(b.p75)}, n=${b.n}${b.unknown ? ` (${b.unknown} unknown)` : ""}`
                          : `no data${b.unknown ? ` (${b.unknown} unknown)` : ""}`}
                      </span>
                    </div>
                  );
                })}
              </>
            )}
            {hover?.type === "point" &&
              (() => {
                const point = series.stages.find((s) => s.key === hover.stage)?.points[hover.index];
                if (!point) return null;
                const pr = point.pr;
                return (
                  <>
                    <strong>{`${pr.repo}#${pr.number}`}</strong>
                    <div>{pr.title}</div>
                    <div className="agg-tip-sub">{style(hover.stage).label}</div>
                    <div>{`${formatDuration(point.seconds)} (${modeLabel.toLowerCase()})`}</div>
                    <div className="agg-tip-sub">
                      {`opened ${formatLocalInstant(Date.parse(pr.opened), timeZone)}`}
                      <br />
                      {`merged ${formatLocalInstant(point.ms, timeZone)}`}
                    </div>
                  </>
                );
              })()}
          </ChartTooltip>
        </div>
      )}
      <div className="agg-summary-wrap">
      <table className="agg-table agg-summary-table">
        <caption>{`Over the whole window (${modeLabel})`}</caption>
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
          {series.stages.map((s) => (
            <tr key={s.key}>
              <th scope="row">{s.label}</th>
              <td>{s.summary.n}</td>
              <td>{s.summary.unknown}</td>
              <td>{dur(s.summary.median)}</td>
              <td>{dur(s.summary.p75)}</td>
              <td>{dur(s.summary.p90)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </ChartFrame>
  );
}

// ---- the status mix: a snapshot of the tasks in flight -------------------------------

/** Below this many tasks in flight (typically) the percentages are coarse. */
const COARSE_BELOW = 5;

export function MixChart({
  title,
  description,
  series,
  shares,
  counts,
  starts,
  inflight,
  timeZone,
  empty,
}: {
  title: string;
  description: ReactNode;
  series: SeriesStyle[];
  /** Per series, the share (0..1) of the tasks in flight at each sampled
   * instant, or null where none was in flight. */
  shares: (number | null)[][];
  /** Per series, how many tasks that is. */
  counts: number[][];
  /** The sampled instants (ISO) and how many tasks were in flight at each. */
  starts: string[];
  inflight: number[];
  /** The viewer's zone: labels and weekend bands (the samples are UTC instants). */
  timeZone: string;
  /** Said when nothing was in flight in the whole window. */
  empty: ReactNode;
}) {
  const [ref, width] = useWidth();
  const id = useId();
  const [hover, setHover] = useState<Hover | null>(null);
  const [mode, setMode] = useState<"lines" | "area">("area");
  const height = 240;
  const stripHeight = 52;
  const plotW = Math.max(width - MARGIN.left - MARGIN.right, 50);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const count = starts.length;
  const times = starts.map((t) => Date.parse(t));
  const span = Math.max(times[count - 1] - times[0], 1);
  const xs = times.map((t) => MARGIN.left + ((t - times[0]) / span) * plotW);
  const x = (i: number): number => xs[i];
  const y = linearScale([0, 1], [MARGIN.top + plotH, MARGIN.top]);
  const hasData = inflight.some((n) => n > 0);
  const last = (() => {
    for (let i = count - 1; i >= 0; i--) if (inflight[i] > 0) return i;
    return -1;
  })();
  const pct = (v: number | null): string => (v === null ? "-" : `${Math.round(v * 100)}%`);
  const typical = medianPositive(inflight);

  if (!hasData) {
    return (
      <ChartFrame title={title} description={description} table={null}>
        <EmptyChart>{empty}</EmptyChart>
      </ChartFrame>
    );
  }

  const at = (index: number): void => setHover({ index, x: xs[index] });
  const move = (event: PointerEvent<SVGRectElement>): void => {
    const box = event.currentTarget.getBoundingClientRect();
    const ms = times[0] + ((event.clientX - box.left) / Math.max(box.width, 1)) * span;
    at(nearestIndex(times, ms));
  };
  const key = (event: KeyboardEvent): void => {
    if (event.key === "Escape") setHover(null);
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const from = hover?.index ?? (step > 0 ? -1 : count);
    at(Math.min(count - 1, Math.max(0, from + step)));
  };

  const latest: Record<string, string> = {};
  series.forEach((s, i) => {
    latest[s.key] = last >= 0 ? pct(shares[i][last]) : "-";
  });
  const summary = `${title}, ${formatLocalDate(times[0], timeZone)} to ${formatLocalDate(times[count - 1], timeZone)}. At the latest sample with tasks in flight (${inflight[last]} tasks): ${series
    .map((s) => `${s.label} ${latest[s.key]}`)
    .join(", ")}.`;
  const bands = stackShares(shares);
  const drawn = series
    .map((s, i) => ({ s, i }))
    .filter(({ i }) => shares[i].some((v) => v !== null && v > 0));
  const stripMax = niceMax(Math.max(...inflight));
  const stripY = linearScale([0, stripMax], [stripHeight - 6, 4]);
  const barW = Math.max(plotW / count, 1);
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const labels = labelIndexes(count, Math.max(2, Math.floor(plotW / 80)));

  return (
    <ChartFrame
      title={title}
      description={description}
      legend={<Legend id={id} series={series} values={latest} />}
      table={
        <table className="agg-table">
          <caption>{`${title}: share (and number) of the tasks in flight at each sampled instant`}</caption>
          <thead>
            <tr>
              <th scope="col">{`Instant (${timeZone})`}</th>
              <th scope="col">Tasks in flight</th>
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
                <th scope="row">{formatLocalInstant(Date.parse(start), timeZone)}</th>
                <td>{inflight[i]}</td>
                {series.map((s, k) => (
                  <td key={s.key}>{shares[k][i] === null ? "-" : `${pct(shares[k][i])} (${counts[k][i]})`}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      <div className="agg-mode" role="group" aria-label={`${title} chart type`}>
        {(
          [
            ["lines", "Lines"],
            ["area", "Stacked area"],
          ] as const
        ).map(([value, label]) => (
          <button key={value} type="button" aria-pressed={mode === value} onClick={() => setMode(value)}>
            {label}
          </button>
        ))}
        {typical < COARSE_BELOW && (
          <span className="agg-coarse">
            {`Typically ${typical} task${typical === 1 ? "" : "s"} in flight: the percentages are coarse.`}
          </span>
        )}
      </div>
      <div className="agg-plot" ref={ref}>
        <svg
          width={width}
          height={height + stripHeight}
          role="img"
          aria-label={summary}
          tabIndex={0}
          onKeyDown={key}
          onBlur={() => setHover(null)}
        >
          <Patterns id={id} series={series} />
          <WeekendPattern id={id} />
          <WeekendBands
            id={id}
            from={times[0]}
            to={times[count - 1]}
            timeZone={timeZone}
            x={(ms) => MARGIN.left + ((ms - times[0]) / span) * plotW}
            top={MARGIN.top}
            bottom={MARGIN.top + plotH}
          />
          {ticks.map((t) => (
            <g key={t}>
              <line className="agg-grid" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={y(t)} y2={y(t)} />
              <text className="agg-tick" x={MARGIN.left - 6} y={y(t)} textAnchor="end" dominantBaseline="middle">
                {`${t * 100}%`}
              </text>
            </g>
          ))}
          {labels.map((i) => (
            <text key={i} className="agg-tick" x={xs[i]} y={height - 10} textAnchor="middle">
              {formatLocalDate(times[i], timeZone)}
            </text>
          ))}
          <text className="agg-axis-title" transform={`translate(11 ${MARGIN.top + plotH / 2}) rotate(-90)`} textAnchor="middle">
            share of tasks in flight
          </text>
          {mode === "area"
            ? series.map((s, i) => (
                <path
                  key={s.key}
                  className={`tone-${s.tone} agg-band`}
                  d={bandPath(bands[i], x, y, Math.max(barW, 2))}
                  fill={fillOf(id, s)}
                />
              ))
            : drawn.map(({ s, i }) => (
                <path
                  key={s.key}
                  className={`tone-${s.tone} agg-line`}
                  d={linePath(shares[i], x, y)}
                  fill="none"
                  stroke="var(--tone)"
                  strokeDasharray={s.hatch ? "5 3" : undefined}
                />
              ))}
          {/* Tasks in flight: n at each sample, on the same time axis. */}
          <g transform={`translate(0 ${height})`}>
            <text className="agg-tick" x={MARGIN.left - 6} y={stripY(0)} textAnchor="end" dominantBaseline="middle">
              0
            </text>
            <text className="agg-tick" x={MARGIN.left - 6} y={stripY(stripMax)} textAnchor="end" dominantBaseline="middle">
              {stripMax}
            </text>
            <text className="agg-axis-title" x={MARGIN.left + plotW} y={2} textAnchor="end" dominantBaseline="hanging">
              tasks in flight
            </text>
            <line className="agg-axis" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={stripY(0)} y2={stripY(0)} />
            {inflight.map((n, i) =>
              n > 0 ? (
                <rect
                  key={starts[i]}
                  className="agg-inflight"
                  x={xs[i] - barW / 2}
                  y={stripY(n)}
                  width={barW}
                  height={stripY(0) - stripY(n)}
                />
              ) : null,
            )}
          </g>
          {hover && (
            <line className="agg-cursor" x1={hover.x} x2={hover.x} y1={MARGIN.top} y2={height + stripHeight - 6} />
          )}
          <rect
            className="agg-hit"
            x={MARGIN.left}
            y={MARGIN.top}
            width={plotW}
            height={plotH + stripHeight}
            onPointerMove={move}
            onPointerLeave={() => setHover(null)}
          />
        </svg>
        <ChartTooltip hover={hover} width={width}>
          {hover && (
            <>
              <strong>{formatLocalInstant(times[hover.index], timeZone)}</strong>
              <div className="agg-tip-sub">
                {inflight[hover.index] > 0
                  ? `${inflight[hover.index]} task${inflight[hover.index] === 1 ? "" : "s"} in flight`
                  : "nothing in flight"}
              </div>
              {[...series]
                .reverse()
                .filter((s) => counts[series.indexOf(s)][hover.index] > 0)
                .map((s) => (
                  <div key={s.key} className="agg-tip-row">
                    <Swatch id={id} series={s} />
                    <span>{s.label}</span>
                    <span className="agg-tip-value">
                      {`${pct(shares[series.indexOf(s)][hover.index])} (${counts[series.indexOf(s)][hover.index]} of ${inflight[hover.index]})`}
                    </span>
                  </div>
                ))}
            </>
          )}
        </ChartTooltip>
      </div>
    </ChartFrame>
  );
}
