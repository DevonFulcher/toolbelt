import { memo, useMemo, type CSSProperties } from "react";
import type { Catalog, LoadedStatus, CiResult } from "../api/generated";
import { formatAbsolute } from "../logic/format";
import {
  eventInfo,
  legendModel,
  listEntries,
  nowChips,
  phaseInfo,
  trackLayout,
  trackStart,
  type Tip,
  type Track,
} from "../logic/timeline";
import { useApp } from "../store/StoreContext";
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
 * One card's timeline. Collapsed: a slim track with the prominent events and
 * nothing else (the card's slots above it already show the current state; the
 * end cap's tooltip lists it). Expanded: a taller track with labelled
 * events, faint commit dots (unless hidden), a labelled "State now" row of
 * chips with the legend toggle ("?") beside it, and the chronological table.
 */
export const Timeline = memo(function Timeline({ id, status, startedAt, catalog, now }: TimelineProps) {
  const ui = useApp((s) => s.cardUi[id]);
  const patchCardUi = useApp((s) => s.patchCardUi);
  const expanded = Boolean(ui?.expanded);
  const hideCommits = Boolean(ui?.hideCommits);
  const legend = Boolean(ui?.legend);
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
  // Expanded only: the labelled chips of the current state, and the legend
  // toggle next to them.
  const stateRow = (
    <div className="tl-state-row">
      <span className="tl-state-label">State now</span>
      <div className="tl-chips" aria-label="State now">
        {chips.map((chip) => (
          <span
            key={chip.dimension}
            className={`tl-chip tone-${chip.tone} tl-chip-${chip.dimension}`}
            title={chip.title}
          >
            <span className="tl-chip-label">{chip.group}</span> {chip.text}
          </span>
        ))}
      </div>
      {legendButton}
    </div>
  );

  if (trackStart(track) === null) {
    // No start recorded and no PR yet, or no history for it: nothing to put on
    // a time axis.
    return (
      <div className={`tl ${expanded ? "tl-expanded" : "tl-collapsed"}`}>
        <div className="tl-body">
          <div className="tl-left">
            <div className="tl-ends">
              {track.state === "none" ? "no PR yet: no history to draw" : "PR history unavailable"}
            </div>
            {!expanded && <span className="tl-buttons">{legendButton}</span>}
          </div>
          {expanded && stateRow}
        </div>
        {legend && <Legend catalog={catalog} />}
      </div>
    );
  }

  const commits = hideCommits ? "none" : expanded ? "all" : "significant";
  const layout = trackLayout(track, {
    now,
    catalog,
    commits,
    laneGap: expanded ? 0.15 : 0.015,
    maxLanes: expanded ? 4 : 2,
  });
  const laneStep = expanded ? 20 : 8;
  const base = expanded ? 34 : 24;
  const height = base + (layout.lanes - 1) * laneStep;
  // The end cap (the "now" line, or the end of a merged/closed PR) says what
  // the state is at that moment.
  const endTip: Extract<Tip, { type: "state" }> = {
    type: "state",
    title: layout.endKind === null ? "Now" : layout.endKind === "merged" ? "Merged" : "Closed",
    ms: layout.end,
    items: chips.map((chip) => `${chip.group}: ${chip.text}`),
  };
  const entries = expanded ? listEntries(track, { now, catalog, commits }) : [];
  const hasCi = entries.some((entry) => entry.ci);

  return (
    <div
      className={`tl ${expanded ? "tl-expanded" : "tl-collapsed"}`}
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
                aria-expanded={expanded}
                onClick={() => patchCardUi(id, { expanded: !expanded })}
              >
                {expanded ? "Collapse" : "Expand"}
              </button>
              {expanded && (
                <button
                  type="button"
                  className="tl-commits"
                  aria-pressed={hideCommits}
                  onClick={() => patchCardUi(id, { hideCommits: !hideCommits })}
                >
                  {hideCommits ? "Show commits" : "Hide commits"}
                </button>
              )}
              {!expanded && legendButton}
            </span>
            <span className="tl-end">{layout.endLabel}</span>
          </div>
          <div
            className="tl-track"
            style={{ height }}
            onClick={() => {
              if (!expanded) patchCardUi(id, { expanded: true });
            }}
          >
            <div className="tl-axis" />
            {layout.ticks.map((tick) => (
              <span key={tick.x} className="tl-tick" style={{ left: `${tick.x * 100}%` }}>
                {expanded ? tick.label : ""}
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
              const label =
                marker.actor && marker.kind.startsWith("review")
                  ? `${info.label} · ${marker.actor}`
                  : info.label;
              return (
                <TipSource
                  key={`${marker.kind}-${marker.ms}-${index}`}
                  tip={marker.tip}
                  label={marker.title}
                  className={`tl-mark tl-k-${marker.kind} tone-${info.tone}${faint ? " tl-faint" : ""}`}
                  style={{
                    left: `${marker.x * 100}%`,
                    ...(faint ? {} : { top: `${(expanded ? 14 : 12) + marker.lane * laneStep}px` }),
                  }}
                >
                  {marker.kind === "commits" ? String(marker.count) : info.glyph}
                  {expanded && !faint && (
                    <span className={`tl-label${marker.x > 0.78 ? " tl-flip" : ""}`}>{label}</span>
                  )}
                </TipSource>
              );
            })}
            <TipSource
              tip={endTip}
              label={`${endTip.title}: ${chips.map((c) => `${c.group} ${c.text}`).join(", ")}`}
              className={layout.now !== null ? "tl-now" : "tl-end-cap"}
            />
          </div>
          {expanded && stateRow}
        </div>
      </div>
      {legend && <Legend catalog={catalog} />}
      {expanded && (
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

/** Everything the track and chips can show, generated from the catalog. */
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
      <section className="tl-legend-part tl-legend-chips">
        <h4>State now (chips in the expanded view)</h4>
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
