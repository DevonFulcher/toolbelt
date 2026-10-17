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
  timeAxis,
  weekendSpans,
} from "../logic/aggregateChart";
import { TOTAL_STAGE, type LeadTimeSeries, type StageKey } from "../logic/leadTime";
import {
  addLocalDays,
  formatLocalDate,
  formatLocalDay,
  formatLocalInstant,
  isWeekend,
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
        <li key={s.key} className="agg-legend-entry" tabIndex={0} aria-describedby={`${id}-legend-${s.key}`}>
          <Swatch id={id} series={s} />
          <span>{s.label}</span>
          {values?.[s.key] !== undefined && <span className="agg-legend-value">{values[s.key]}</span>}
          <span id={`${id}-legend-${s.key}`} className="agg-legend-help" role="tooltip">{s.legend}</span>
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

function WeekendControl({ excluded, onChange }: { excluded: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="agg-check">
      <input type="checkbox" checked={excluded} onChange={(event) => onChange(event.target.checked)} />
      Exclude weekends
    </label>
  );
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
      {weekendSpans(from, to, timeZone).map(([start, end]) => {
        const left = x(start);
        const width = Math.max(x(end) - left, 0);
        return (
          <g key={start}>
            <rect
              className="agg-weekend"
              x={left}
              y={top}
              width={width}
              height={bottom - top}
              fill={`url(#${id}-weekend)`}
            />
          </g>
        );
      })}
    </g>
  );
}

/** "Weekend: Oct 10 to Oct 11", or one date for a weekend the window clips to a single day. */
function weekendTitle(start: number, end: number, timeZone: string): string {
  const first = formatLocalDate(start, timeZone);
  const last = formatLocalDate(end - 1, timeZone);
  return first === last ? `Weekend: ${first}` : `Weekend: ${first} to ${last}`;
}

// ---- throughput ----------------------------------------------------------------------------

export interface ThroughputSeries {
  opened: SeriesStyle;
  merged: SeriesStyle;
}

export function ThroughputChart({
  data,
  weekdayData,
  timeZone,
  styles,
}: {
  data: Throughput;
  weekdayData: Throughput;
  timeZone: string;
  styles: ThroughputSeries;
}) {
  const [ref, width] = useWidth();
  const id = useId();
  const [hover, setHover] = useState<Hover | null>(null);
  const [excludeWeekends, setExcludeWeekends] = useState(false);
  const { buckets, opened, merged, closed, granularity } = excludeWeekends ? weekdayData : data;
  const daily = granularity === "day";
  const indexes = buckets.map((_, i) => i).filter((i) => !excludeWeekends || !daily || !buckets[i].weekend);
  const shown = indexes.map((i) => buckets[i]);
  const height = 220;
  const plotW = Math.max(width - MARGIN.left - MARGIN.right, 50);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const max = Math.max(1, ...indexes.map((i) => opened[i]), ...indexes.map((i) => merged[i]));
  const ticks = countTicks(max);
  const top = niceMax(ticks[ticks.length - 1]);
  const y = linearScale([0, top], [MARGIN.top + plotH, MARGIN.top]);
  const slot = plotW / Math.max(shown.length, 1);
  const barW = Math.max(Math.min(slot * 0.4, 22), 1);
  const centre = (i: number): number => MARGIN.left + slot * (i + 0.5);
  const totalOpened = indexes.reduce((a, i) => a + opened[i], 0);
  const totalMerged = indexes.reduce((a, i) => a + merged[i], 0);
  const labelled = slot >= 14;
  const first = buckets[0].start;
  const lastEnd = buckets[buckets.length - 1].end;
  const bucketX = (ms: number): number => MARGIN.left + ((ms - first) / Math.max(lastEnd - first, 1)) * plotW;
  const dates = daily
    ? dayLabels(shown, timeZone, slot, excludeWeekends ? 5 : 7)
    : shown.map((b, i) =>
        labelIndexes(shown.length, Math.max(2, Math.floor(plotW / 70))).includes(i)
          ? formatLocalDate(b.start, timeZone)
          : null,
      );
  const title = (i: number): string =>
    daily ? formatLocalDay(shown[i].start, timeZone) : `Week of ${formatLocalDay(shown[i].start, timeZone)}`;

  const move = (event: PointerEvent<SVGRectElement>): void => {
    const box = event.currentTarget.getBoundingClientRect();
    const index = Math.min(shown.length - 1, Math.max(0, Math.floor((event.clientX - box.left) / slot)));
    setHover({ index, x: centre(index) });
  };
  const key = (event: KeyboardEvent): void => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (event.key === "Escape") setHover(null);
    if (!step) return;
    event.preventDefault();
    const next = Math.min(
      shown.length - 1,
      Math.max(0, (hover?.index ?? (step > 0 ? -1 : shown.length)) + step),
    );
    setHover({ index: next, x: centre(next) });
  };

  const summary = `${totalOpened} PRs opened and ${totalMerged} merged over ${shown.length} ${daily ? "days" : "weeks"}, from ${formatLocalDay(first, timeZone)}${excludeWeekends && daily ? ", excluding weekends" : ""}.`;
  const both = [styles.opened, styles.merged];
  return (
    <ChartFrame
      title="Throughput"
      description={
        daily
          ? `PRs opened and merged per day in your time zone (${timeZone}); ${excludeWeekends ? "weekend days are removed from the chart and totals. The last bar is the latest weekday in the window" : "Saturdays and Sundays are hatched. The last day is today so far"}.`
          : `PRs opened and merged per week in your time zone (${timeZone}); weeks start on Monday. ${excludeWeekends ? "Weekend events are removed from the chart and totals." : "Each week spans a weekend, so no weekends are marked."} The last is the current week so far.`
      }
      legend={<div className="agg-lead-controls">
        <Legend
          id={id}
          series={both}
          values={{ opened: `${totalOpened} total`, merged: `${totalMerged} total` }}
        />
        <WeekendControl excluded={excludeWeekends} onChange={(value) => { setExcludeWeekends(value); setHover(null); }} />
      </div>}
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
            {shown.map((bucket, position) => {
              const i = indexes[position];
              return (
                <tr key={bucket.start}>
                  <th scope="row">{formatLocalDay(bucket.start, timeZone)}</th>
                  {daily && <td>{bucket.weekend ? "yes" : ""}</td>}
                  <td>{opened[i]}</td>
                  <td>{merged[i]}</td>
                  <td>{closed[i]}</td>
                </tr>
              );
            })}
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
          {daily && !excludeWeekends && (
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
          {indexes.map((sourceIndex, i) => (
            <g key={buckets[sourceIndex].start} className={hover?.index === i ? "agg-col agg-col-active" : "agg-col"}>
              {([
                [styles.opened, opened[sourceIndex], -barW],
                [styles.merged, merged[sourceIndex], 0],
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
              {labelled && merged[sourceIndex] > 0 && (
                <text className="agg-value" x={centre(i) + barW / 2} y={y(merged[sourceIndex]) - 3} textAnchor="middle">
                  {merged[sourceIndex]}
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
              {shown[hover.index].weekend && <div className="agg-tip-sub">weekend</div>}
              <div>{`${opened[indexes[hover.index]]} opened`}</div>
              <div>{`${merged[indexes[hover.index]]} merged`}</div>
              <div>{`${closed[indexes[hover.index]]} closed unmerged`}</div>
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
  const eventMode = series.granularity === "event";
  const bandShown = !eventMode && band && total !== undefined && !hidden.has(TOTAL_STAGE);
  const values = [
    ...visible.flatMap((s) => eventMode ? s.points.map((p) => p.seconds) : s.buckets.map((b) => b.median ?? 0)),
    ...(bandShown && total ? total.buckets.map((b) => b.p90 ?? 0) : []),
  ];
  const axis = durationAxis(Math.max(0, ...values));
  const y = linearScale([0, axis.top], [MARGIN.top + plotH, MARGIN.top]);
  const pointY = (seconds: number): number => y(Math.min(seconds, axis.top));
  const project = timeAxis(windowStart, now, timeZone, excludeWeekends);
  const xMs = (ms: number): number => MARGIN.left + project(ms) * plotW;
  const centres = series.buckets.map((b) => xMs(eventMode ? b.start : (b.start + Math.min(b.end, now)) / 2));
  const bucketX = (i: number): number => centres[i];
  const axisLabels = eventMode
    ? series.buckets.reduce<number[]>((picked, bucket, i) => {
        if ((!excludeWeekends || !isWeekend(bucket.start, timeZone)) &&
          (!picked.length || xMs(bucket.start) - xMs(series.buckets[picked[picked.length - 1]].start) >= 70)) picked.push(i);
        return picked;
      }, [])
    : labelIndexes(series.buckets.length, Math.max(2, Math.floor(plotW / 70)));
  const style = (key: StageKey): LeadStageStyle => styles.find((x) => x.key === key) as LeadStageStyle;
  const label = (ms: number): string => formatLocalDate(ms, timeZone);
  const rangeLabel = (i: number): string => {
    const b = series.buckets[i];
    if (eventMode) return `${formatLocalInstant(b.start, timeZone)}${b.pr ? ` · ${b.pr.repo}#${b.pr.number}` : ""}`;
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
    const px = MARGIN.left + ((event.clientX - box.left) / Math.max(box.width, 1)) * plotW;
    const index = nearestIndex(centres, px);
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
        eventMode
          ? `Each point is one PR at its exact merge time (${timeZone}), using ${modeLabel.toLowerCase()}. Lines connect known values for each step; missing milestones leave gaps. Hover a point for its PR and duration.${excludeWeekends ? " Weekend hours have zero width on the x-axis." : ""}`
          : `Each PR is placed at the time it merged (${timeZone}), and each line is the median of a step over the PRs that merged in each week, using ${modeLabel.toLowerCase()}. A bucket with no PR whose step is known is a gap. The dots are single PRs; hover one for its details. The scale follows the medians; longer individual PRs appear at the top edge with their full duration in the tooltip.${excludeWeekends ? " Weekend hours have zero width on the x-axis." : ""}`
      }
      legend={
        <div className="agg-lead-controls">
          <ul className="agg-legend">
            {styles.map((s) => (
              <li key={s.key} className="agg-legend-entry">
                <button
                  type="button"
                  className="agg-legend-toggle"
                  aria-describedby={`${id}-legend-${s.key}`}
                  aria-pressed={!hidden.has(s.key)}
                  onClick={() => toggle(s.key)}
                >
                  <Swatch id={id} series={s} />
                  <span>{s.label}</span>
                </button>
                <span id={`${id}-legend-${s.key}`} className="agg-legend-help" role="tooltip">{s.legend}</span>
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
            {!eventMode && <label className="agg-check">
              <input type="checkbox" checked={band} onChange={(event) => setBand(event.target.checked)} />
              p75 to p90 band (total)
            </label>}
          </div>
        </div>
      }
      table={
        <table className="agg-table">
          <caption>{eventMode ? `Lead time (${modeLabel}) per merged PR` : `Lead time (${modeLabel}) per bucket: median (PRs known / unknown)`}</caption>
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
                  return <td key={s.key}>{eventMode ? dur(b.median) : b.n > 0 ? `${dur(b.median)} (${b.n} / ${b.unknown})` : `- (0 / ${b.unknown})`}</td>;
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
            {!excludeWeekends && <WeekendBands
              id={id}
              from={windowStart}
              to={now}
              timeZone={timeZone}
              x={xMs}
              top={MARGIN.top}
              bottom={MARGIN.top + plotH}
            />}
            {axis.ticks.map((t) => (
              <g key={t.value}>
                <line className="agg-grid" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={y(t.value)} y2={y(t.value)} />
                <text className="agg-tick" x={MARGIN.left - 6} y={y(t.value)} textAnchor="end" dominantBaseline="middle">
                  {t.label}
                </text>
              </g>
            ))}
            {axisLabels.map((i) => (
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
                      cy={pointY(point.seconds)}
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
                        {eventMode
                          ? dur(b.median)
                          : b.n > 0
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
  transitionTicks,
}: {
  title: string;
  description: ReactNode;
  series: SeriesStyle[];
  /** Per series, the share (0..1) of the tasks in flight at each transition
   * instant, or null where none was in flight. */
  shares: (number | null)[][];
  /** Per series, how many tasks that is. */
  counts: number[][];
  /** The transition instants (ISO) and how many tasks were in flight at each. */
  starts: string[];
  inflight: number[];
  /** The viewer's zone: labels and weekend bands (the instants are UTC). */
  timeZone: string;
  /** Said when nothing was in flight in the whole window. */
  empty: ReactNode;
  /** Mark each exact transition time on the x-axis for short windows. */
  transitionTicks: boolean;
}) {
  const [ref, width] = useWidth();
  const id = useId();
  const [hover, setHover] = useState<Hover | null>(null);
  const [excludeWeekends, setExcludeWeekends] = useState(false);
  // Each band is a share of the tasks in flight (summing to 100%), or how many
  // tasks that is (the whole stack is the number in flight).
  const [scale, setScale] = useState<"share" | "count">("share");
  const height = 240;
  const plotW = Math.max(width - MARGIN.left - MARGIN.right, 50);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const count = starts.length;
  const times = starts.map((t) => Date.parse(t));
  const project = timeAxis(times[0], times[count - 1], timeZone, excludeWeekends);
  const xs = times.map((t) => MARGIN.left + project(t) * plotW);
  const x = (i: number): number => xs[i];
  const countMax = niceMax(Math.max(...inflight, 1));
  const y = linearScale([0, scale === "share" ? 1 : countMax], [MARGIN.top + plotH, MARGIN.top]);
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
    const px = MARGIN.left + ((event.clientX - box.left) / Math.max(box.width, 1)) * plotW;
    at(nearestIndex(xs, px));
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
  const summary = `${title}, ${formatLocalDate(times[0], timeZone)} to ${formatLocalDate(times[count - 1], timeZone)}. At the latest change with tasks in flight (${inflight[last]} tasks): ${series
    .map((s) => `${s.label} ${latest[s.key]}`)
    .join(", ")}.`;
  const bands = stackShares(
    scale === "share" ? shares : counts.map((row) => row.map((n, i) => (inflight[i] > 0 ? n : null))),
  );
  const barW = Math.max(plotW / count, 1);
  // Whole numbers only: quarters of the axis when they come out whole, else halves.
  const ticks =
    scale === "share"
      ? [0, 0.25, 0.5, 0.75, 1]
      : (countMax % 4 === 0 ? [0, 1, 2, 3, 4].map((i) => (countMax * i) / 4) : [0, countMax / 2, countMax]).filter(
          Number.isInteger,
        );
  const labels: number[] = [];
  xs.forEach((at, i) => {
    if ((!excludeWeekends || !isWeekend(times[i], timeZone)) &&
      (labels.length === 0 || at - xs[labels[labels.length - 1]] >= 80)) labels.push(i);
  });

  return (
    <ChartFrame
      title={title}
      description={<>{description}{excludeWeekends && " Weekend hours have zero width on the x-axis; use the arrow keys to inspect changes that share an x position."}</>}
      legend={<div className="agg-lead-controls">
        <Legend id={id} series={series} values={latest} />
        <div className="agg-seg" role="group" aria-label="Scale">
          <button type="button" aria-pressed={scale === "share"} onClick={() => setScale("share")}>
            Share of tasks
          </button>
          <button type="button" aria-pressed={scale === "count"} onClick={() => setScale("count")}>
            Number of tasks
          </button>
        </div>
        <WeekendControl excluded={excludeWeekends} onChange={(value) => { setExcludeWeekends(value); setHover(null); }} />
      </div>}
      table={
        <table className="agg-table">
          <caption>{`${title}: share (and number) of the tasks in flight at each state change`}</caption>
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
      {scale === "share" && typical < COARSE_BELOW && (
        <div className="agg-mode">
          <span className="agg-coarse">
            {`Typically ${typical} task${typical === 1 ? "" : "s"} in flight: the percentages are coarse.`}
          </span>
        </div>
      )}
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
          <WeekendPattern id={id} />
          {ticks.map((t) => (
            <g key={t}>
              <line className="agg-grid" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={y(t)} y2={y(t)} />
              <text className="agg-tick" x={MARGIN.left - 6} y={y(t)} textAnchor="end" dominantBaseline="middle">
                {scale === "share" ? `${t * 100}%` : String(t)}
              </text>
            </g>
          ))}
          {labels.map((i) => (
            <text key={i} className="agg-tick" x={xs[i]} y={height - 10} textAnchor="middle">
              {formatLocalDate(times[i], timeZone)}
            </text>
          ))}
          <text className="agg-axis-title" transform={`translate(11 ${MARGIN.top + plotH / 2}) rotate(-90)`} textAnchor="middle">
            {scale === "share" ? "share of tasks in flight" : "tasks in flight"}
          </text>
          {series.map((s, i) => (
            <path
              key={s.key}
              className={`tone-${s.tone} agg-band`}
              d={bandPath(bands[i], x, y, Math.max(barW, 2))}
              fill={fillOf(id, s)}
            />
          ))}
          {transitionTicks && xs.slice(1, -1).map((at, i) => (
            <line key={starts[i + 1]} className="agg-transition-tick" x1={at} x2={at} y1={MARGIN.top + plotH} y2={MARGIN.top + plotH + 5} />
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
          {/* Weekends: a thin strip on the time axis, after the hit area so it can be hovered. */}
          {!excludeWeekends && weekendSpans(times[0], times[count - 1], timeZone).map(([start, end]) => (
            <rect
              key={start}
              className="agg-weekend-strip"
              x={MARGIN.left + project(start) * plotW}
              y={MARGIN.top + plotH + 1}
              width={Math.max((project(end) - project(start)) * plotW, 0)}
              height={4}
            >
              <title>{weekendTitle(start, end, timeZone)}</title>
            </rect>
          ))}
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

// ---- stacked bars over time ---------------------------------------------------------------

/** Bars one per stretch of time, each stacked from the series' values (money,
 * counts: anything that adds up). */
export function StackedBarsChart({
  title,
  description,
  series,
  starts,
  values,
  bucketHours,
  timeZone,
  format,
  yLabel,
  empty,
}: {
  title: string;
  description: ReactNode;
  series: SeriesStyle[];
  /** ISO start of each bucket. */
  starts: string[];
  /** Per series, its value in each bucket. */
  values: number[][];
  /** How long a bucket is: an hour, or a day (24). */
  bucketHours: number;
  timeZone: string;
  /** A value as text (axis, tooltip, table). */
  format: (value: number) => string;
  yLabel: string;
  /** Said when every value is zero. */
  empty: ReactNode;
}) {
  const [ref, width] = useWidth();
  const id = useId();
  const [hover, setHover] = useState<Hover | null>(null);
  const height = 220;
  const plotW = Math.max(width - MARGIN.left - MARGIN.right, 50);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const count = starts.length;
  const times = starts.map((t) => Date.parse(t));
  const totals = starts.map((_, i) => values.reduce((sum, row) => sum + row[i], 0));
  const seriesTotals = values.map((row) => row.reduce((sum, v) => sum + v, 0));
  if (count === 0 || totals.every((t) => t === 0)) {
    return (
      <ChartFrame title={title} description={description} table={null}>
        <EmptyChart>{empty}</EmptyChart>
      </ChartFrame>
    );
  }
  const max = niceMax(Math.max(...totals));
  const y = linearScale([0, max], [MARGIN.top + plotH, MARGIN.top]);
  const slot = plotW / count;
  const barW = Math.max(slot * 0.7, 1);
  const centre = (i: number): number => MARGIN.left + slot * (i + 0.5);
  const label = (i: number): string =>
    bucketHours === 1 ? formatLocalInstant(times[i], timeZone) : formatLocalDate(times[i], timeZone);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => t * max);
  const labelled: number[] = [];
  for (let i = 0; i < count; i++) {
    if (labelled.length === 0 || centre(i) - centre(labelled[labelled.length - 1]) >= 80) labelled.push(i);
  }
  const at = (index: number): void => setHover({ index, x: centre(index) });
  const move = (event: PointerEvent<SVGRectElement>): void => {
    const box = event.currentTarget.getBoundingClientRect();
    const px = ((event.clientX - box.left) / Math.max(box.width, 1)) * plotW;
    at(Math.min(count - 1, Math.max(0, Math.floor(px / slot))));
  };
  const key = (event: KeyboardEvent): void => {
    if (event.key === "Escape") setHover(null);
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const from = hover?.index ?? (step > 0 ? -1 : count);
    at(Math.min(count - 1, Math.max(0, from + step)));
  };
  const legendValues: Record<string, string> = {};
  series.forEach((s, k) => {
    legendValues[s.key] = format(seriesTotals[k]);
  });
  const summary = `${title}, ${label(0)} to ${label(count - 1)}: ${series
    .map((s, k) => `${s.label} ${format(seriesTotals[k])}`)
    .join(", ")}.`;

  return (
    <ChartFrame
      title={title}
      description={description}
      legend={<Legend id={id} series={series} values={legendValues} />}
      table={
        <table className="agg-table">
          <caption>{`${title}: what each ${bucketHours === 1 ? "hour" : "day"} cost`}</caption>
          <thead>
            <tr>
              <th scope="col">{`${bucketHours === 1 ? "Hour" : "Day"} (${timeZone})`}</th>
              {series.map((s) => (
                <th key={s.key} scope="col">
                  {s.label}
                </th>
              ))}
              <th scope="col">Total</th>
            </tr>
          </thead>
          <tbody>
            {starts.map((start, i) => (
              <tr key={start}>
                <th scope="row">{label(i)}</th>
                {values.map((row, k) => (
                  <td key={series[k].key}>{format(row[i])}</td>
                ))}
                <td>{format(totals[i])}</td>
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
                {format(t)}
              </text>
            </g>
          ))}
          {labelled.map((i) => (
            <text key={starts[i]} className="agg-tick" x={centre(i)} y={height - 10} textAnchor="middle">
              {label(i)}
            </text>
          ))}
          <text className="agg-axis-title" transform={`translate(11 ${MARGIN.top + plotH / 2}) rotate(-90)`} textAnchor="middle">
            {yLabel}
          </text>
          {starts.map((start, i) => {
            let bottom = 0;
            return (
              <g key={start}>
                {series.map((s, k) => {
                  const value = values[k][i];
                  const rect = (
                    <rect
                      key={s.key}
                      className={`tone-${s.tone} agg-bar`}
                      x={centre(i) - barW / 2}
                      y={y(bottom + value)}
                      width={barW}
                      height={y(bottom) - y(bottom + value)}
                      fill={fillOf(id, s)}
                    />
                  );
                  bottom += value;
                  return value > 0 ? rect : null;
                })}
              </g>
            );
          })}
          {hover && <line className="agg-cursor" x1={hover.x} x2={hover.x} y1={MARGIN.top} y2={MARGIN.top + plotH} />}
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
              <strong>{label(hover.index)}</strong>
              <div className="agg-tip-sub">{`${format(totals[hover.index])} in all`}</div>
              {[...series]
                .reverse()
                .filter((s) => values[series.indexOf(s)][hover.index] > 0)
                .map((s) => (
                  <div key={s.key} className="agg-tip-row">
                    <Swatch id={id} series={s} />
                    <span>{s.label}</span>
                    <span className="agg-tip-value">{format(values[series.indexOf(s)][hover.index])}</span>
                  </div>
                ))}
            </>
          )}
        </ChartTooltip>
      </div>
    </ChartFrame>
  );
}
