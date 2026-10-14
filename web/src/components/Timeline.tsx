import { memo, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { Catalog, LoadedStatus, CiResult } from "../api/generated";
import { formatAbsolute } from "../logic/format";
import {
  endTip,
  eventInfo,
  legendModel,
  listEntries,
  nowChips,
  phaseInfo,
  trackLayout,
  trackStart,
  type Track,
} from "../logic/timeline";
import { useApp } from "../store/StoreContext";
import { DayGrid } from "./DayGrid";
import { TipSource } from "./Tooltip";

/**
 * What the track of a loaded status is drawn from. `startedAt` is when the
 * task started (the branch's own, `null` when it predates tracking it).
 */
export function trackOf(status: LoadedStatus, startedAt: string | null): Track {
  const { detail } = status;
  return {
    // A PR whose history is unavailable has nothing to put the start next to.
    started: detail || status.pr === "none" ? startedAt : null,
    created: detail ? detail.created : null,
    ended: detail ? detail.ended : null,
    state: status.pr,
    draft: detail ? detail.is_draft : false,
    review: status.review,
    events: detail ? detail.events : [],
    current: status.now,
  };
}

/** The track's height with one lane of markers, and what each further lane adds (px). */
const TRACK_BASE = 34;
const LANE_STEP = 20;
/** Where the first lane's markers sit from the track's top (px). */
const MARKER_TOP = 14;

const CI_WORDS: Record<Exclude<CiResult, "">, string> = {
  failed: "failed",
  success: "passed",
  pending: "pending",
};

interface TimelineProps {
  /** The branch's card id (its UI state is kept in the store). */
  id: string;
  status: LoadedStatus;
  /** When the branch's task started (null: not recorded). */
  startedAt: string | null;
  catalog: Catalog;
  /** Current time (ms); a new minute re-draws. */
  now: number;
}

/**
 * One card's timeline: a labelled track (events in lanes, faint commit dots
 * unless hidden, axis ticks, phases, weekday lines) whose end cap's tooltip
 * lists the current state, the legend toggle ("?") and, behind the
 * Expand/Collapse button, the chronological log table.
 */
/** The pixel width of an element, kept current as it resizes. 0 until measured. */
function useWidth(ref: React.RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setWidth(el.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

export const Timeline = memo(function Timeline({ id, status, startedAt, catalog, now }: TimelineProps) {
  const ui = useApp((s) => s.cardUi[id]);
  const patchCardUi = useApp((s) => s.patchCardUi);
  const logOpen = useApp((s) => s.logOpen.includes(id));
  const toggleLog = useApp((s) => s.toggleLog);
  const hideCommits = Boolean(ui?.hideCommits);
  const legend = Boolean(ui?.legend);
  const trackRef = useRef<HTMLDivElement>(null);
  const trackWidth = useWidth(trackRef);
  const track = useMemo(() => trackOf(status, startedAt), [status, startedAt]);
  const chips = useMemo(() => nowChips(track, catalog), [track, catalog]);

  const legendButton = (
    <button
      type="button"
      className="tl-legend-toggle"
      title="What do the glyphs, colours and spans mean?"
      aria-pressed={legend}
      onClick={() => patchCardUi(id, { legend: !legend })}
    >
      ?
    </button>
  );
  if (trackStart(track) === null) {
    // No start recorded and no PR yet, or no history for it: nothing to put on
    // a time axis.
    return (
      <div className="tl">
        <div className="tl-body">
          <div className="tl-left">
            <div className="tl-ends">
              {track.state === "none" ? "no PR yet: no history to draw" : "PR history unavailable"}
              <span className="tl-buttons">{legendButton}</span>
            </div>
          </div>
        </div>
        {legend && <Legend catalog={catalog} />}
      </div>
    );
  }

  const commits = hideCommits ? "none" : "all";
  const layout = trackLayout(track, {
    now,
    catalog,
    commits,
    laneGap: 0.15,
    maxLanes: 4,
    ...(trackWidth > 0 ? { widthPx: trackWidth } : {}),
  });
  const height = TRACK_BASE + (layout.lanes - 1) * LANE_STEP;
  // The end cap (the "now" line, or the end of a merged/closed PR) says what
  // the state is at that moment.
  const capTip = endTip(layout, chips);
  const entries = logOpen ? listEntries(track, { now, catalog, commits }) : [];
  const hasCi = entries.some((entry) => entry.ci);

  return (
    <div
      className="tl"
      style={{ "--tl-height": `${height}px` } as CSSProperties}
    >
      <div className="tl-body">
        <div className="tl-left">
          <div className="tl-ends">
            <span className="tl-start">{layout.startLabel}</span>
            <span className="tl-buttons">
              <button
                type="button"
                className="tl-expand"
                aria-expanded={logOpen}
                onClick={() => toggleLog(id)}
              >
                {logOpen ? "Collapse" : "Expand"}
              </button>
              <button
                type="button"
                className="tl-commits"
                aria-pressed={hideCommits}
                onClick={() => patchCardUi(id, { hideCommits: !hideCommits })}
              >
                {hideCommits ? "Show commits" : "Hide commits"}
              </button>
              {legendButton}
            </span>
            <span className="tl-end">{layout.endLabel}</span>
          </div>
          <div className="tl-track" style={{ height }} ref={trackRef}>
            <DayGrid start={layout.start} end={layout.end} />
            <div className="tl-axis" />
            {layout.ticks.map((tick) => (
              <span key={tick.x} className="tl-tick" style={{ left: `${tick.x * 100}%` }}>
                {tick.label}
              </span>
            ))}
            {layout.phases.map((phase) => (
              <TipSource
                key={`${phase.kind}-${phase.t0}`}
                tip={phase.tip}
                label={phase.title}
                className={`tl-phase tl-phase-${phase.kind} tone-${phaseInfo(catalog, phase.kind).tone}`}
                style={{
                  left: `${phase.x0 * 100}%`,
                  width: `${Math.max((phase.x1 - phase.x0) * 100, 0.4)}%`,
                }}
              />
            ))}
            {layout.markers.map((marker, index) => {
              const faint = marker.kind === "commit" || marker.kind === "commits";
              const info = eventInfo(catalog, marker.kind);
              return (
                <TipSource
                  key={`${marker.kind}-${marker.ms}-${index}`}
                  tip={marker.tip}
                  label={marker.title}
                  className={`tl-mark tl-k-${marker.kind} tone-${info.tone}${faint ? " tl-faint" : ""}`}
                  style={{
                    left: `${marker.x * 100}%`,
                    ...(faint ? {} : { top: `${MARKER_TOP + marker.lane * LANE_STEP}px` }),
                  }}
                >
                  {marker.kind === "commits" ? String(marker.count) : info.glyph}
                  {!faint && !marker.labelHidden && (
                    <span
                      className={`tl-label${marker.flip ? " tl-flip" : ""}`}
                      style={{ "--x": marker.x } as CSSProperties}
                    >
                      {marker.label}
                    </span>
                  )}
                </TipSource>
              );
            })}
            <TipSource
              tip={capTip}
              label={`${capTip.title}: ${chips.map((c) => `${c.group} ${c.text}`).join(", ")}`}
              className={layout.now !== null ? "tl-now" : "tl-end-cap"}
            />
          </div>
        </div>
      </div>
      {legend && <Legend catalog={catalog} />}
      {logOpen && (
        <div className={`tl-list-wrap${hasCi ? " tl-has-ci" : ""}`}>
          <table className="tl-list">
            <thead>
              <tr>
                <th className="tl-h-time">Time</th>
                <th className="tl-h-event">Event</th>
                <th className="tl-h-actor">Actor</th>
                <th className="tl-h-detail">Detail</th>
                {hasCi && <th className="tl-h-ci">CI</th>}
              </tr>
            </thead>
            <tbody>
              {entries.map((entry, index) => (
                <tr key={`${entry.t}-${index}`} className={`tl-item tl-i-${entry.kind}`}>
                  <td className="tl-when">
                    <time dateTime={entry.t} title={formatAbsolute(entry.ms)}>
                      {entry.age}
                    </time>
                  </td>
                  <td className="tl-what">{entry.label}</td>
                  <td className="tl-who">{entry.actor}</td>
                  <td className="tl-detail" title={entry.detail}>
                    {entry.detail}
                  </td>
                  {hasCi && (
                    <td className={`tl-ci tl-ci-${entry.ci || "none"}`}>
                      {entry.ci ? CI_WORDS[entry.ci as Exclude<CiResult, "">] : ""}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
});

/** Everything the track and its end-cap tooltip can show, generated from the catalog. */
export function Legend({ catalog }: { catalog: Catalog }) {
  const model = useMemo(() => legendModel(catalog), [catalog]);
  return (
    <div className="tl-legend">
      <section className="tl-legend-part">
        <h4>Markers</h4>
        {model.events.map((info) => (
          <div className="tl-legend-row" key={info.kind}>
            {info.kind === "commits" ? (
              <span className={`tl-sample tl-k-commits tone-${info.tone}`}>N</span>
            ) : (
              <span className={`tl-sample tone-${info.tone}`}>{info.glyph}</span>
            )}
            <b>{info.label}</b>
            <span>{info.legend}</span>
          </div>
        ))}
      </section>
      <section className="tl-legend-part">
        <h4>Shaded spans</h4>
        {model.phases.map((info) => (
          <div className="tl-legend-row" key={info.kind}>
            <span className={`tl-sample tl-swatch tone-${info.tone}`} />
            <b>{info.label}</b>
            <span>{info.legend}</span>
          </div>
        ))}
      </section>
      <section className="tl-legend-part">
        <h4>Days (decoration)</h4>
        <div className="tl-legend-row">
          <span className="tl-sample tl-swatch-day" />
          <b>Weekday line</b>
          <span>A line at each local midnight, labelled with the weekday that starts there (every day, or only Mondays when crowded).</span>
        </div>
        <div className="tl-legend-row">
          <span className="tl-sample tl-swatch-weekend" />
          <b>Weekend</b>
          <span>Saturday 00:00 to Monday 00:00, local time. Not a state; it only helps read durations.</span>
        </div>
      </section>
      <section className="tl-legend-part tl-legend-chips">
        <h4>State now (listed in the tooltip of the track's right end: Now, Merged or Closed)</h4>
        {model.chips.flatMap((group) =>
          group.values.map((value) => (
            <div className="tl-legend-row" key={`${group.dimension}-${value.text}`}>
              <span className={`tl-sample tl-chip tone-${value.tone}`}>
                <span className="tl-chip-label">{group.group}</span> {value.text}
              </span>
              <b />
              <span>{value.legend}</span>
            </div>
          )),
        )}
      </section>
      <section className="tl-legend-part">
        <h4>Not shown</h4>
        {model.notRepresented.map((item) => (
          <div className="tl-legend-row" key={item.label}>
            <span className="tl-sample">–</span>
            <b>{item.label}</b>
            <span>{item.why}</span>
          </div>
        ))}
      </section>
    </div>
  );
}
