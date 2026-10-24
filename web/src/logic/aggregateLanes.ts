// The aggregate view's lane layers: for each lane the server counted, how
// often its spans were on the tasks in flight at each sampled instant. A layer
// is drawn like the status mix (a stack of shares that sums to 100%), with the
// rest of the tasks as "no <lane> activity". Labels, tones and legends come
// from the catalog's lanes, so a lane or kind added on the server is drawn
// without per-kind code.
import type { Aggregate, Catalog, LaneEventKind, LaneId, LanePhaseKind, Tone } from "../api/generated";

export const NO_ACTIVITY = "none";

export interface LaneLayerStyle {
  key: string;
  label: string;
  tone: Tone;
  legend: string;
}

export interface LaneLayer {
  lane: LaneId;
  label: string;
  styles: LaneLayerStyle[];
  shares: (number | null)[][];
  counts: number[][];
  /** Markers of the lane in the window, with how many there were. */
  events: { kind: LaneEventKind; label: string; count: number }[];
}

/** The lane every task is always in exactly one span of (its status). */
export const STATUS_LANE: LaneId = "next";

/**
 * The layers worth drawing: lanes with a span on some task in flight or a
 * marker in the window (lanes without any are left out: nothing was
 * recorded), and always the status lane, whose spans tile each task's life, so
 * it has no "no activity" remainder.
 */
export function laneLayers(data: Aggregate, catalog: Catalog): LaneLayer[] {
  const { inflight } = data.samples;
  const specs = new Map(catalog.lanes.map((lane) => [lane.id, lane]));
  const layers: LaneLayer[] = [];
  for (const lane of data.lanes) {
    const spec = specs.get(lane.lane);
    if (!spec) continue;
    const events = lane.events
      .filter((e) => e.times.length > 0)
      .map((e) => ({ kind: e.kind, label: catalog.lane_events[e.kind].label, count: e.times.length }));
    const used = lane.phases.filter((p) => p.count.some((n) => n > 0));
    const status = lane.lane === STATUS_LANE;
    if (!status && used.length === 0 && events.length === 0) continue;
    const styles: LaneLayerStyle[] = used.map((p) => {
      const info = catalog.lane_phases[p.kind as LanePhaseKind];
      return { key: p.kind, label: info.label, tone: info.tone, legend: info.legend };
    });
    if (status) {
      // Every kind, in the catalog's stacking order, so the legend is complete.
      const all = lane.phases;
      layers.push({
        lane: lane.lane,
        label: spec.label,
        styles: all.map((p) => {
          const info = catalog.lane_phases[p.kind as LanePhaseKind];
          return { key: p.kind, label: info.label, tone: info.tone, legend: info.legend };
        }),
        shares: all.map((p) => p.share),
        counts: all.map((p) => p.count),
        events,
      });
      continue;
    }
    const sums = inflight.map((_, i) => used.reduce((total, p) => total + p.count[i], 0));
    layers.push({
      lane: lane.lane,
      label: spec.label,
      styles: [
        ...styles,
        {
          key: NO_ACTIVITY,
          label: `No ${spec.label.toLowerCase()} activity`,
          tone: "gray",
          legend: `Tasks in flight with nothing recorded in the ${spec.label} lane at that moment.`,
        },
      ],
      shares: [
        ...used.map((p) => p.share),
        inflight.map((n, i) => (n > 0 ? Math.max(1 - sums[i] / n, 0) : null)),
      ],
      counts: [...used.map((p) => p.count), inflight.map((n, i) => Math.max(n - sums[i], 0))],
      events,
    });
  }
  return layers;
}
