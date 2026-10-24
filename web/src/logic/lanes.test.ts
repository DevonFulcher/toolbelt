import { describe, expect, test } from "vitest";
import type { TimelineLane } from "../api/generated";
import { ago, catalog, DAY, HOUR, NOW } from "../testing/fixtures";
import { laneLegend, laneRows } from "./lanes";

const start = NOW - 10 * DAY;
const iso = (ms: number): string => new Date(ms).toISOString();

const lanes: TimelineLane[] = [
  {
    lane: "claim",
    events: [],
    phases: [
      { start: iso(NOW - 4 * DAY), end: iso(NOW - 3 * DAY), kind: "claim_agent_worktree", detail: "me: /w" },
      { start: iso(NOW - HOUR), end: null, kind: "claim_autonomous_ref", detail: "" },
    ],
  },
  {
    lane: "git",
    events: [],
    phases: [{ start: iso(start - DAY), end: iso(start + DAY), kind: "git_conflicting", detail: "" }],
  },
  {
    lane: "slack",
    events: [
      { t: iso(NOW - 2 * DAY), kind: "slack_post", detail: "Pat" },
      { t: iso(start - DAY), kind: "slack_bump", detail: "" },
    ],
    phases: [],
  },
];

describe("laneRows", () => {
  const rows = laneRows(lanes, catalog, start, NOW, NOW);

  test("lists the lanes in the catalog's order", () => {
    expect(rows.map((r) => r.lane)).toEqual(["git", "claim", "slack"]);
    expect(rows.map((r) => r.lane)).toEqual(
      catalog.lanes.map((l) => l.id).filter((id) => ["git", "claim", "slack"].includes(id)),
    );
  });

  test("scales items on the track's axis and clips what straddles its start", () => {
    const git = rows[0]!.phases[0]!;
    expect(git.x0).toBe(0);
    expect(git.x1).toBeCloseTo(0.1);
    const claim = rows[1]!.phases[0]!;
    expect(claim.x0).toBeCloseTo(0.6);
    expect(claim.x1).toBeCloseTo(0.7);
  });

  test("runs a span with no end to now and marks its tooltip ongoing", () => {
    const open = rows[1]!.phases[1]!;
    expect(open.x1).toBe(1);
    expect(open.tip).toMatchObject({ type: "phase", ongoing: true });
  });

  test("leaves out an event before the track starts", () => {
    expect(rows[2]!.events.map((e) => e.kind)).toEqual(["slack_post"]);
  });

  test("takes glyph, tone and wording from the catalog", () => {
    const post = rows[2]!.events[0]!;
    expect(post.glyph).toBe(catalog.lane_events.slack_post.glyph);
    expect(post.tip).toMatchObject({ type: "event", detail: "Pat", ms: NOW - 2 * DAY });
    expect(ago(DAY)).toBeTruthy();
  });
});

describe("laneLegend", () => {
  test("lists every kind of the catalog under exactly one lane", () => {
    const legend = laneLegend(catalog);
    expect(legend.flatMap((l) => l.events.map((e) => e.kind)).sort()).toEqual(
      Object.keys(catalog.lane_events).sort(),
    );
    expect(legend.flatMap((l) => l.phases.map((p) => p.kind)).sort()).toEqual(
      Object.keys(catalog.lane_phases).sort(),
    );
  });
});
