// The geometry of a card's timeline: a task's life as a horizontal,
// time-scaled track from when it started (when known; else from the PR's
// creation) to now (or to its merge/close), with shaded phases, event markers
// (plain commits clustered where they would overlap), axis ticks, an end cap
// whose tooltip lists the current state, hover tooltips and the legend.
//
// Everything the track can draw (glyphs, labels, tones, chip texts, the
// legend) comes from the state catalog the server sends, so a card state
// cannot exist without a timeline representation and the legend cannot drift
// from what is drawn. Pure functions; the components draw their result.
import type {
  ActivityKind,
  Catalog,
  CatalogState,
  Chip,
  EventKind,
  PhaseKind,
  PrState,
  ReviewState,
  TimelineEvent,
  Tone,
  Who,
} from "../api/generated";
import { assertNever } from "./exhaustive";
import { DAY, formatAbsolute, formatAge, formatSpan, HOUR, MINUTE, relative } from "./format";

/**
 * What a track is drawn from: when the task started (`null`: not recorded),
 * a PR's history (`created: null` is a branch with no PR, or no history) and
 * the current state ("now"). A track needs a start or a creation time to be drawn.
 */
export interface Track {
  /** When the task started (the branch was created); null when unknown. */
  started: string | null;
  created: string | null;
  /** When it was merged/closed; null while open. */
  ended: string | null;
  state: PrState;
  /** Whether it is a draft right now. */
  draft: boolean;
  /** The current review state (decides whether an approval went stale). */
  review?: ReviewState;
  events: TimelineEvent[];
  current: Chip[];
}

// ---- catalog lookups ------------------------------------------------------------

export function eventInfo(catalog: Catalog, kind: EventKind) {
  const info = catalog.events[kind] as Catalog["events"][EventKind] | undefined;
  if (!info) throw new Error(`timeline: no catalog entry for event kind "${kind}"`);
  return info;
}

export function phaseInfo(catalog: Catalog, kind: PhaseKind) {
  const info = catalog.phases[kind] as Catalog["phases"][PhaseKind] | undefined;
  if (!info) throw new Error(`timeline: no catalog entry for phase kind "${kind}"`);
  return info;
}

export function stateInfo(catalog: Catalog, dimension: string, value: string): CatalogState {
  const info = catalog.states.find((s) => s.dimension === dimension && s.value === value);
  if (!info) throw new Error(`timeline: no catalog entry for ${dimension}=${value}`);
  return info;
}

/** How a server event maps to a marker kind (a key of the catalog's events). */
export function markerKind(event: Pick<TimelineEvent, "kind" | "ci" | "detail">): EventKind {
  const kind: ActivityKind = event.kind;
  switch (kind) {
    case "commit":
      return event.ci === "failed" ? "commit_failed" : "commit";
    case "review":
      if (event.detail === "approved") return "review_approved";
      if (event.detail === "requested changes") return "review_changes";
      return "review_commented";
    case "opened":
    case "ready_for_review":
    case "converted_to_draft":
    case "review_requested":
    case "review_request_removed":
    case "force_push":
    case "merged":
    case "closed":
    case "reopened":
      return kind;
    default:
      return assertNever(kind, "event kind");
  }
}

type StartFields = Pick<Track, "events"> & Partial<Pick<Track, "started" | "created">>;

/**
 * The ms a track begins at: the earliest of the task's start, the PR's
 * creation and the earliest timeline event (the recorded start can be later
 * than reality, e.g. a branch registered after its first commits). `null`
 * when there is nothing to anchor a track to.
 */
export function trackStart(track: StartFields): number | null {
  const times = [track.started, track.created, ...track.events.map((e) => e.t)]
    .filter((t): t is string => typeof t === "string")
    .map((t) => Date.parse(t));
  return times.length ? Math.min(...times) : null;
}

/**
 * The ms the "task started" marker sits at: the track's start, when it is
 * before the PR was opened (or there is no PR); null when the PR's opening
 * is the start.
 */
export function workingStart(track: StartFields): number | null {
  const start = trackStart(track);
  if (start === null) return null;
  return !track.created || start < Date.parse(track.created) ? start : null;
}

const isCommit = (kind: EventKind): boolean => kind === "commit" || kind === "commit_failed";
const clamp01 = (value: number): number => Math.min(1, Math.max(0, value));

// ---- phases ----------------------------------------------------------------------

interface TimedEvent extends TimelineEvent {
  ms: number;
  seq: number;
  /** When the commit's checks completed, ms (null: not known). */
  ciDone: number | null;
}

/** The phases of a PR's life (the working phase is drawn before it). */
type PrPhaseKind = Exclude<PhaseKind, "working">;

export interface PhaseSpan {
  kind: PhaseKind;
  t0: number;
  t1: number;
}

/**
 * Which spans of the PR's life were spent in draft, waiting for a reviewer,
 * approved, or with failing CI, in ms. Nothing starts before `start` (the
 * PR's creation): the working phase before it is added by `trackLayout`.
 *
 * - draft: from creation (when the first draft toggle is "ready for review",
 *   or the PR is a draft now and was never toggled) or each "back to draft",
 *   until "ready for review".
 * - waiting: not a draft and at least one review request outstanding (a
 *   review by the reviewer, or removing the request, settles it).
 * - approved: from an approving review while any approval stands: until
 *   merge/close/now, a later "changes requested" or dismissal by that
 *   reviewer, or, when the PR is open and not approved now (so the approval
 *   went stale), the first push after it.
 * - ci_running ("Waiting on CI"): from a push until that commit's checks
 *   completed, whatever the outcome; until the next push (or the end) when
 *   they have not and the commit's rollup is pending.
 * - ci_failing: from the completion of a failing commit's checks until the
 *   next commit or force push (which supersedes it), or the end of the PR.
 *   A failed commit with no known completion time fails from its push (a
 *   fallback: GitHub gave no suite times).
 *
 * Mirrors `replay` / `commit_ci_phases` in the server (`aggregate.py`,
 * `pr_activity.py`); `phaseVectors.json` is asserted by both.
 */
export function commitCiPhases(
  result: string,
  push: number,
  done: number | null,
): ("running" | "failing")[] {
  if (result === "") return [];
  if (done !== null && done > push) return result === "failed" ? ["running", "failing"] : ["running"];
  if (result === "failed") return ["failing"];
  if (result === "pending" && done === null) return ["running"];
  return [];
}

export function phaseSpans(
  events: {
    kind: ActivityKind;
    actor: string;
    subject: string;
    detail: string;
    ci: string;
    ms: number;
    /** When the commit's checks completed (ms), if known. */
    ciDone?: number | null;
  }[],
  track: Pick<Track, "state" | "draft" | "review">,
  start: number,
  end: number,
): PhaseSpan[] {
  const toggles = events.filter(
    (e) => e.kind === "ready_for_review" || e.kind === "converted_to_draft",
  );
  const done = track.state === "merged" || track.state === "closed";
  const staleOnPush = !done && Boolean(track.review) && track.review !== "approved";
  let draft = toggles.length ? toggles[0].kind === "ready_for_review" : Boolean(track.draft);
  const pending = new Set<string>();
  const approvers = new Set<string>();
  let failing = false;
  let running = false;
  // Whether the head commit's checks fail once they complete, and which
  // event (by index) is the head.
  let failsWhenDone = false;
  let head = -1;
  const open: Record<PrPhaseKind, number | null> = {
    draft: draft ? start : null,
    waiting: null,
    approved: null,
    ci_running: null,
    ci_failing: null,
  };
  const spans: PhaseSpan[] = [];
  const set = (kind: PrPhaseKind, on: boolean, ms: number): void => {
    const t = Math.min(Math.max(ms, start), end);
    const began = open[kind];
    if (on && began === null) {
      open[kind] = t;
    } else if (!on && began !== null) {
      if (t > began) spans.push({ kind, t0: began, t1: t });
      open[kind] = null;
    }
  };
  // Each commit's checks completing is a step of its own.
  type Step = Omit<(typeof events)[number], "kind"> & { kind: ActivityKind | "ci_done"; id: number };
  const steps: Step[] = events.map((event, id) => ({ ...event, id }));
  events.forEach((event, id) => {
    const done = event.ciDone ?? null;
    if (event.kind === "commit" && done !== null && event.ms < done && done <= end) {
      steps.push({ ...event, kind: "ci_done", ms: done, id });
    }
  });
  steps.sort((a, b) => a.ms - b.ms);
  for (const event of steps) {
    const who = event.subject || event.actor;
    if (event.kind === "ready_for_review") draft = false;
    else if (event.kind === "converted_to_draft") draft = true;
    else if (event.kind === "review_requested") pending.add(who);
    else if (event.kind === "review_request_removed") pending.delete(who);
    else if (event.kind === "review") {
      pending.delete(who);
      if (event.detail === "approved") approvers.add(who);
      else if (event.detail === "requested changes" || event.detail === "review dismissed") {
        approvers.delete(who);
      }
    } else if (event.kind === "commit" || event.kind === "force_push") {
      if (staleOnPush) approvers.clear();
      const phases = event.kind === "commit" ? commitCiPhases(event.ci, event.ms, event.ciDone ?? null) : [];
      running = phases[0] === "running";
      failing = phases[0] === "failing";
      failsWhenDone = phases[1] === "failing";
      head = event.kind === "commit" ? event.id : -1;
    } else if (event.kind === "ci_done") {
      if (event.id !== head) continue;
      running = false;
      failing = failsWhenDone;
    } else continue;
    set("draft", draft, event.ms);
    set("waiting", pending.size > 0 && !draft, event.ms);
    set("approved", approvers.size > 0, event.ms);
    set("ci_running", running, event.ms);
    set("ci_failing", failing, event.ms);
  }
  for (const kind of Object.keys(open) as PrPhaseKind[]) {
    const began = open[kind];
    if (began !== null && end > began) spans.push({ kind, t0: began, t1: end });
  }
  return spans.sort((a, b) => a.t0 - b.t0);
}

// ---- axis ---------------------------------------------------------------------------

const TICK_STEPS = [
  5 * MINUTE,
  15 * MINUTE,
  HOUR,
  6 * HOUR,
  DAY,
  3 * DAY,
  7 * DAY,
  14 * DAY,
  28 * DAY,
];

export interface Tick {
  x: number;
  label: string;
}

/**
 * Axis ticks at "nice" intervals back from the end, labelled with their age.
 * Ticks too near either end (where the end labels are) are dropped.
 */
export function axisTicks(start: number, end: number, now: number, maxTicks = 6): Tick[] {
  const span = end - start;
  const step = TICK_STEPS.find((s) => span / s <= maxTicks) ?? TICK_STEPS[TICK_STEPS.length - 1];
  const ticks: Tick[] = [];
  for (let t = end - step; t > start; t -= step) {
    const x = (t - start) / span;
    if (x > 0.06 && x < 0.94) ticks.push({ x, label: formatAge((now - t) / 1000) });
  }
  return ticks.reverse();
}

// ---- track layout ---------------------------------------------------------------------

export interface TipItem {
  t: string;
  ms: number;
  actor: string;
  detail: string;
}

/** What a hover tooltip is built from (see `tooltipContent`). */
export type Tip =
  | {
      type: "event";
      kind: EventKind;
      label: string;
      detail: string;
      actor: string;
      subject: string;
      ci: string;
      ms: number;
    }
  | { type: "cluster"; label: string; items: TipItem[]; first: number; last: number }
  /** The track's end cap: the current states at that moment. */
  | { type: "state"; title: string; ms: number; items: string[] }
  | {
      type: "phase";
      kind: PhaseKind;
      label: string;
      legend: string;
      t0: number;
      t1: number;
      ongoing: boolean;
    };

export interface Marker {
  kind: EventKind;
  x: number;
  ms: number;
  t: string;
  actor: string;
  detail: string;
  ci: string;
  count: number;
  lane: number;
  /** The text drawn beside the marker (empty for faint commit dots). */
  label: string;
  /** The label sits left of its marker (the marker is past the middle). */
  flip: boolean;
  /** No lane had room for the label at this width: the marker is drawn alone. */
  labelHidden: boolean;
  items: TipItem[];
  title: string;
  tip: Tip;
}

export interface Phase {
  kind: PhaseKind;
  label: string;
  x0: number;
  x1: number;
  t0: string;
  t1: string;
  title: string;
  tip: Tip;
}

export interface TrackLayout {
  start: number;
  end: number;
  span: number;
  ended: boolean;
  markers: Marker[];
  phases: Phase[];
  lanes: number;
  /** 1 (the right end) while the PR is open, else null. */
  now: number | null;
  startLabel: string;
  /** How the PR ended (null while open). */
  endKind: "merged" | "closed" | null;
  endLabel: string;
  ticks: Tick[];
}

export interface LayoutOptions {
  catalog: Catalog;
  /** Current time, ms. */
  now: number;
  /**
   * "none" (no commits at all), "significant" (only commits whose CI failed)
   * or "all" (ordinary commits as faint dots, clustered where they would
   * overlap).
   */
  commits?: "none" | "significant" | "all";
  /** Fraction of the track under which commits cluster. */
  minGap?: number;
  /** Fraction of the track two prominent markers need to share a lane. */
  laneGap?: number;
  /** Most lanes markers stack into. */
  maxLanes?: number;
  /**
   * The track's width in pixels. When given, labels are placed by their real
   * width: a marker takes the first lane whose labels it does not touch, and
   * is drawn without its label when no lane has room. Without it lanes are
   * decided by `laneGap` alone (a fraction of the track).
   */
  widthPx?: number;
}

/** A marker past this fraction of the track puts its label on its left. */
export const LABEL_FLIP_AT = 0.5;
// Rough metrics of the 11px label text and the marker glyph, in pixels.
const LABEL_CHAR_PX = 6.2;
const LABEL_PAD_PX = 8;
const LABEL_GLYPH_HALF_PX = 7;
const LABEL_GAP_PX = 4;
const LABEL_MIN_PX = 24;

/**
 * Lays out a track. Every marker and phase carries `tip`, the data its hover
 * tooltip is built from.
 */
export function trackLayout(track: Track, options: LayoutOptions): TrackLayout {
  const { now, catalog } = options;
  const commits = options.commits ?? "significant";
  const minGap = options.minGap ?? 0.012;
  const laneGap = options.laneGap ?? 0.015;
  const maxLanes = options.maxLanes ?? 3;

  const trackStartMs = trackStart(track);
  if (trackStartMs === null) throw new Error("timeline: a track needs a task start or a PR creation time");
  const start = trackStartMs;
  // The task's working phase runs from the start until the PR opened.
  const startedMs = workingStart(track);
  // When the PR was opened; none for a branch that has no PR yet.
  const prStart = track.created === null ? null : Date.parse(track.created);
  const events: TimedEvent[] = track.events
    .map((event, seq) => ({
      ...event,
      ms: Date.parse(event.t),
      seq,
      ciDone: event.ci_done ? Date.parse(event.ci_done) : null,
    }))
    .sort((a, b) => a.ms - b.ms || a.seq - b.seq);
  const done = track.state === "merged" || track.state === "closed";
  let endMs = track.ended ? Date.parse(track.ended) : null;
  if (endMs === null && done) {
    // A finished PR whose end event fell out of the history we have.
    const last = events.length ? events[events.length - 1].ms : start;
    endMs = Math.min(Math.max(last, start), now);
  }
  const ended = endMs !== null;
  const end = endMs !== null ? Math.max(endMs, start) : Math.max(now, start);
  const span = Math.max(end - start, MINUTE);
  const xOf = (ms: number): number => clamp01((ms - start) / span);
  const age = (ms: number): string => `${formatAge((now - ms) / 1000)} ago`;
  const endKind = ended ? (track.state === "closed" ? "closed" : "merged") : null;

  // One marker per event, then drop and cluster plain commits.
  const prominent: Marker[] = [];
  const plain: Marker[] = [];
  if (startedMs !== null) {
    const info = eventInfo(catalog, "started");
    prominent.push({
      kind: "started",
      x: 0,
      ms: startedMs,
      t: new Date(startedMs).toISOString(),
      actor: "",
      detail: "",
      ci: "",
      count: 1,
      lane: 0,
      label: "",
      flip: false,
      labelHidden: false,
      items: [],
      title: `${info.label} ${age(startedMs)}`,
      tip: {
        type: "event",
        kind: "started",
        label: info.label,
        detail: "",
        actor: "",
        subject: "",
        ci: "",
        ms: startedMs,
      },
    });
  }
  for (const event of events) {
    const kind = markerKind(event);
    const info = eventInfo(catalog, kind);
    if (isCommit(kind) && commits === "none") continue;
    const withDetail = event.detail && kind !== "opened" && event.kind !== "review";
    const marker: Marker = {
      kind,
      x: xOf(event.ms),
      ms: event.ms,
      t: event.t,
      actor: event.actor,
      detail: event.detail,
      ci: event.ci || "",
      count: 1,
      lane: 0,
      label: "",
      flip: false,
      labelHidden: false,
      items: [{ t: event.t, ms: event.ms, actor: event.actor, detail: event.detail }],
      title:
        `${info.label}${withDetail ? `: ${event.detail}` : ""}` +
        `${event.actor ? ` (${event.actor})` : ""} ${age(event.ms)}`,
      tip: {
        type: "event",
        kind,
        label: info.label,
        detail: event.detail,
        actor: event.actor,
        subject: event.subject || "",
        ci: event.ci || "",
        ms: event.ms,
      },
    };
    if (kind === "commit") {
      if (commits === "all") plain.push(marker);
    } else {
      prominent.push(marker);
    }
  }

  const clusters: { members: Marker[]; lastX: number }[] = [];
  for (const marker of plain) {
    const previous = clusters[clusters.length - 1];
    if (previous && marker.x - previous.lastX < minGap) {
      previous.members.push(marker);
      previous.lastX = marker.x;
    } else {
      clusters.push({ members: [marker], lastX: marker.x });
    }
  }
  const faint: Marker[] = clusters.map(({ members }) => {
    if (members.length === 1) return members[0];
    const first = members[0];
    const last = members[members.length - 1];
    const x = members.reduce((sum, m) => sum + m.x, 0) / members.length;
    const items = members.flatMap((m) => m.items);
    return {
      ...first,
      kind: "commits",
      x,
      count: members.length,
      detail: `${members.length} commits`,
      items,
      title: `${members.length} commits, ${age(first.ms)} to ${age(last.ms)}`,
      tip: {
        type: "cluster",
        label: `${members.length} commits`,
        items,
        first: first.ms,
        last: last.ms,
      },
    };
  });

  // Prominent markers that would overlap stack into lanes.
  const lastX: number[] = [];
  const taken: Array<Array<[number, number]>> = [];
  const widthPx = options.widthPx;
  for (const marker of prominent) {
    const info = eventInfo(catalog, marker.kind);
    marker.label =
      marker.actor && marker.kind.startsWith("review") ? `${info.label} · ${marker.actor}` : info.label;
    marker.flip = marker.x > LABEL_FLIP_AT;
    if (widthPx !== undefined && widthPx > 0) {
      // Pixel mode: the label (cut to the room on its side) and the glyph
      // occupy an interval; the marker takes the first lane it does not touch.
      const x = marker.x * widthPx;
      const room = marker.flip ? x : widthPx - x;
      const text = Math.min(
        marker.label.length * LABEL_CHAR_PX,
        Math.max(room - LABEL_PAD_PX - LABEL_GLYPH_HALF_PX, LABEL_MIN_PX),
      );
      const left = marker.flip ? x - LABEL_PAD_PX - text : x - LABEL_GLYPH_HALF_PX;
      const right = marker.flip ? x + LABEL_GLYPH_HALF_PX : x + LABEL_PAD_PX + text;
      const free = (lane: Array<[number, number]>) =>
        lane.every(([l, r]) => right + LABEL_GAP_PX <= l || left - LABEL_GAP_PX >= r);
      let lane = taken.findIndex(free);
      if (lane === -1 && taken.length < maxLanes) {
        taken.push([]);
        lane = taken.length - 1;
      }
      if (lane === -1) {
        marker.labelHidden = true;
        marker.lane = 0;
      } else {
        taken[lane].push([left, right]);
        marker.lane = lane;
      }
      continue;
    }
    let lane = lastX.findIndex((x) => marker.x - x >= laneGap);
    if (lane === -1) {
      lane = lastX.length < maxLanes ? lastX.length : lastX.indexOf(Math.min(...lastX));
    }
    lastX[lane] = marker.x;
    marker.lane = lane;
  }

  const spans: PhaseSpan[] = [];
  if (startedMs !== null) {
    // Working: from the task's start until the PR opened (or until now).
    const workingEnd = prStart === null ? end : Math.min(prStart, end);
    if (workingEnd > startedMs) spans.push({ kind: "working", t0: startedMs, t1: workingEnd });
  }
  if (prStart !== null) spans.push(...phaseSpans(events, track, prStart, end));
  const phases: Phase[] = spans.map((p) => {
    const info = phaseInfo(catalog, p.kind);
    return {
      kind: p.kind,
      label: info.label,
      x0: xOf(p.t0),
      x1: xOf(p.t1),
      t0: new Date(p.t0).toISOString(),
      t1: new Date(p.t1).toISOString(),
      title: `${info.label} for ${formatAge((p.t1 - p.t0) / 1000)}`,
      tip: {
        type: "phase",
        kind: p.kind,
        label: info.label,
        legend: info.legend,
        t0: p.t0,
        t1: p.t1,
        ongoing: !ended && p.t1 >= end,
      },
    };
  });

  const markers = [...prominent, ...faint].sort((a, b) => a.x - b.x || a.ms - b.ms);
  return {
    start,
    end,
    span,
    ended,
    markers,
    phases,
    lanes: Math.max(1, lastX.length, taken.length),
    now: ended ? null : 1,
    startLabel: `started ${age(start)}`,
    endKind,
    endLabel: endKind ? `${endKind} ${age(end)}` : "now",
    ticks: axisTicks(start, end, now),
  };
}

// ---- current state ---------------------------------------------------------------------------

export interface NowChip {
  dimension: Chip["dimension"];
  group: string;
  text: string;
  tone: Tone;
  title: string;
}

// The Next chip is toned by who it waits on.
const WHO_TONES: Record<Who, Tone> = {
  you: "accent",
  agent: "purple",
  ci: "amber",
  others: "gray",
  none: "gray",
};

/**
 * The current state of a track (what the end cap's tooltip lists), each looked up in the catalog (an unknown
 * value throws). The Next chip says the action's own label (which names a
 * parent) and is toned by who it waits on.
 */
export function nowChips(track: Pick<Track, "current">, catalog: Catalog): NowChip[] {
  return track.current.map((chip): NowChip => {
    const info = stateInfo(catalog, chip.dimension, chip.value);
    const group = catalog.chips[chip.dimension];
    switch (chip.dimension) {
      case "pr":
      case "ci":
      case "review":
      case "merge":
        return { dimension: chip.dimension, group, text: info.label, tone: info.tone, title: info.legend };
      case "comments":
        return {
          dimension: chip.dimension,
          group,
          text: info.label.replace("{n}", String(chip.n)),
          tone: info.tone,
          title: info.legend,
        };
      case "next":
        return {
          dimension: chip.dimension,
          group,
          text: chip.text || info.label,
          tone: WHO_TONES[chip.who],
          title: info.legend,
        };
      default:
        return assertNever(chip, "chip dimension");
    }
  });
}

/**
 * The tooltip of the track's end cap (the "now" line, or the end of a
 * merged/closed PR): the current state, one "Group: value" line per state.
 * Every catalog state that has a `now_state` representation appears here.
 */
export function endTip(
  layout: Pick<TrackLayout, "endKind" | "end">,
  states: NowChip[],
): Extract<Tip, { type: "state" }> {
  return {
    type: "state",
    title: layout.endKind === null ? "Now" : layout.endKind === "merged" ? "Merged" : "Closed",
    ms: layout.end,
    items: states.map((state) => `${state.group}: ${state.text}`),
  };
}

// ---- tooltips -------------------------------------------------------------------------------

export interface TooltipContent {
  title: string;
  subtitle: string;
  rows: [string, string][];
  items: string[];
}

/**
 * What a marker's / span's tooltip says, with the absolute time (viewer's
 * timezone) and the relative one.
 */
export function tooltipContent(
  tip: Tip,
  now: number,
  options: { timeZone?: string } = {},
): TooltipContent {
  const abs = (ms: number): string => formatAbsolute(ms, options);
  const both = (ms: number): string => `${abs(ms)} (${relative(ms, now)})`;
  if (tip.type === "event") {
    const rows: [string, string][] = [];
    if (tip.actor) rows.push(["Actor", tip.actor]);
    if (tip.subject && tip.subject !== tip.actor) rows.push(["Reviewer", tip.subject]);
    if (tip.ci) rows.push(["CI", tip.ci]);
    rows.push(["When", abs(tip.ms)], ["Ago", relative(tip.ms, now)]);
    return {
      title: tip.label,
      subtitle: tip.detail && tip.detail !== tip.label ? tip.detail : "",
      rows,
      items: [],
    };
  }
  if (tip.type === "state") {
    return {
      title: tip.title,
      subtitle: "",
      rows: [
        ["When", abs(tip.ms)],
        ["Ago", relative(tip.ms, now)],
      ],
      items: tip.items,
    };
  }
  if (tip.type === "cluster") {
    const shown = tip.items.slice(0, 8);
    const items = shown.map((item) => `${abs(item.ms)} · ${item.actor || "?"} · ${item.detail || ""}`);
    if (tip.items.length > shown.length) items.push(`+ ${tip.items.length - shown.length} more`);
    return {
      title: tip.label,
      subtitle: "",
      rows: [
        ["From", both(tip.first)],
        ["To", both(tip.last)],
      ],
      items,
    };
  }
  return {
    title: tip.label,
    subtitle: tip.legend,
    rows: [
      ["From", both(tip.t0)],
      ["To", tip.ongoing ? "now" : both(tip.t1)],
      ["Duration", formatSpan((tip.t1 - tip.t0) / 1000)],
    ],
    items: [],
  };
}

export interface Rect {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

/**
 * Where to put a tooltip of `size` next to `anchor` (a viewport rect), below
 * it when it fits (else above), centred and clamped to the viewport.
 */
export function positionTip(
  anchor: Rect,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
  gap = 8,
): { left: number; top: number; placement: "below" | "above" } {
  const margin = 4;
  const centre = (anchor.left + anchor.right) / 2;
  let left = centre - size.width / 2;
  left = Math.min(left, viewport.width - size.width - margin);
  left = Math.max(left, margin);
  const below = anchor.bottom + gap;
  const above = anchor.top - gap - size.height;
  let top = below;
  let placement: "below" | "above" = "below";
  if (below + size.height > viewport.height - margin && above >= margin) {
    top = above;
    placement = "above";
  }
  top = Math.max(margin, Math.min(top, viewport.height - size.height - margin));
  return { left, top, placement };
}

// ---- legend ----------------------------------------------------------------------------------

export interface LegendModel {
  events: (Catalog["events"][EventKind] & { kind: EventKind })[];
  phases: (Catalog["phases"][PhaseKind] & { kind: PhaseKind })[];
  chips: {
    dimension: Chip["dimension"];
    group: string;
    values: { text: string; tone: Tone; legend: string }[];
  }[];
  notRepresented: Catalog["not_represented"];
}

/** Everything the track and its end-cap tooltip can show, from the catalog. */
export function legendModel(catalog: Catalog): LegendModel {
  return {
    events: (Object.entries(catalog.events) as [EventKind, Catalog["events"][EventKind]][]).map(
      ([kind, info]) => ({ kind, ...info }),
    ),
    phases: (Object.entries(catalog.phases) as [PhaseKind, Catalog["phases"][PhaseKind]][]).map(
      ([kind, info]) => ({ kind, ...info }),
    ),
    chips: (Object.entries(catalog.chips) as [Chip["dimension"], string][]).map(
      ([dimension, group]) => ({
        dimension,
        group,
        values: catalog.states
          .filter((s) => s.dimension === dimension)
          .map((s) => ({ text: s.label, tone: s.tone, legend: s.legend })),
      }),
    ),
    notRepresented: catalog.not_represented,
  };
}

// ---- list ---------------------------------------------------------------------------------------

export interface ListEntry {
  t: string;
  ms: number;
  kind: EventKind;
  label: string;
  actor: string;
  detail: string;
  ci: string;
  age: string;
}

/**
 * The plain chronological list: every event (commits unless "none"), oldest
 * first, starting with the task's start when it is known.
 */
export function listEntries(
  track: StartFields,
  options: { now: number; catalog: Catalog; commits?: "none" | "significant" | "all" },
): ListEntry[] {
  const { now, catalog } = options;
  const hideCommits = options.commits === "none";
  const startedMs = workingStart(track);
  const started: ListEntry[] =
    startedMs === null
      ? []
      : [
          {
            t: new Date(startedMs).toISOString(),
            ms: startedMs,
            kind: "started",
            label: eventInfo(catalog, "started").label,
            actor: "",
            detail: "",
            ci: "",
            age: relative(startedMs, now),
          },
        ];
  const events = track.events
    .map((event, seq) => ({ event, seq, ms: Date.parse(event.t) }))
    .sort((a, b) => a.ms - b.ms || a.seq - b.seq)
    .filter(({ event }) => !(hideCommits && event.kind === "commit"))
    .map(({ event, ms }): ListEntry => {
      const kind = markerKind(event);
      return {
        t: event.t,
        ms,
        kind,
        label: eventInfo(catalog, kind).label,
        actor: event.actor,
        detail: event.detail,
        ci: event.ci || "",
        age: relative(ms, now),
      };
    });
  return [...started, ...events].sort((a, b) => a.ms - b.ms);
}
