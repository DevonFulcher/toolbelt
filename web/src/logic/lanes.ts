// The lanes under a card's PR track: one thin row per source of status that
// has something to show (git conflicts, observed CI, agent runs, claims, Slack,
// the merge queue, decisions, the task tracker), on the same time axis as the
// track. Which lanes and kinds exist, and how each looks, comes from the
// catalog the server sends (`catalog.lanes`, `lane_events`, `lane_phases`), so
// a status added on the server is drawn here without per-kind code.
import type {
  Catalog,
  LaneEventKind,
  LaneId,
  LanePhaseKind,
  TimelineLane,
  Tone,
} from "../api/generated";
import type { Tip } from "./timeline";

export interface LanePhaseDraw {
  kind: LanePhaseKind;
  x0: number;
  x1: number;
  tone: Tone;
  title: string;
  tip: Tip;
}

export interface LaneEventDraw {
  kind: LaneEventKind;
  x: number;
  glyph: string;
  tone: Tone;
  title: string;
  tip: Tip;
}

export interface LaneRow {
  lane: LaneId;
  label: string;
  phases: LanePhaseDraw[];
  events: LaneEventDraw[];
}

/**
 * The rows to draw under a track that runs from `start` to `end` (ms). An item
 * outside that range is left out, one that straddles it is clipped, and a span
 * with no end runs to `now` (or the track's end, when that is earlier).
 */
export function laneRows(
  lanes: readonly TimelineLane[],
  catalog: Catalog,
  start: number,
  end: number,
  now: number,
): LaneRow[] {
  const span = end - start;
  if (span <= 0) return [];
  const x = (ms: number): number => (ms - start) / span;
  const byId = new Map(lanes.map((lane) => [lane.lane, lane]));
  const rows: LaneRow[] = [];
  for (const spec of catalog.lanes) {
    const lane = byId.get(spec.id);
    if (!lane) continue;
    const phases: LanePhaseDraw[] = [];
    for (const phase of lane.phases) {
      const t0 = Date.parse(phase.start);
      const ongoing = phase.end === null;
      const t1 = phase.end === null ? Math.min(now, end) : Date.parse(phase.end);
      if (t1 < start || t0 > end) continue;
      const info = catalog.lane_phases[phase.kind];
      phases.push({
        kind: phase.kind,
        x0: Math.max(x(t0), 0),
        x1: Math.min(x(t1), 1),
        tone: info.tone,
        title: info.label,
        tip: {
          type: "phase",
          kind: phase.kind,
          label: phase.detail ? `${info.label}: ${phase.detail}` : info.label,
          legend: info.legend,
          t0,
          t1,
          ongoing,
        },
      });
    }
    const events: LaneEventDraw[] = [];
    for (const event of lane.events) {
      const ms = Date.parse(event.t);
      if (ms < start || ms > end) continue;
      const info = catalog.lane_events[event.kind];
      events.push({
        kind: event.kind,
        x: x(ms),
        glyph: info.glyph,
        tone: info.tone,
        title: info.label,
        tip: {
          type: "event",
          kind: event.kind,
          label: info.label,
          detail: event.detail,
          actor: "",
          subject: "",
          ci: "",
          ms,
        },
      });
    }
    if (phases.length || events.length) {
      rows.push({ lane: spec.id, label: spec.label, phases, events });
    }
  }
  return rows;
}

export interface LaneLegend {
  lane: LaneId;
  label: string;
  events: { kind: LaneEventKind; glyph: string; tone: Tone; label: string; legend: string }[];
  phases: { kind: LanePhaseKind; tone: Tone; label: string; legend: string }[];
}

/** Each lane's markers and spans, for the legend. */
export function laneLegend(catalog: Catalog): LaneLegend[] {
  return catalog.lanes.map((spec) => ({
    lane: spec.id,
    label: spec.label,
    events: spec.events.map((kind) => ({ kind, ...catalog.lane_events[kind] })),
    phases: spec.phases.map((kind) => ({ kind, ...catalog.lane_phases[kind] })),
  }));
}
