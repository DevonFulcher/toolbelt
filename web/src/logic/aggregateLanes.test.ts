import { describe, expect, test } from "vitest";
import type { Aggregate } from "../api/generated";
import { aggregate } from "../testing/aggregateFixtures";
import { catalog } from "../testing/fixtures";
import { laneLayers, NO_ACTIVITY } from "./aggregateLanes";

const base = aggregate();
const withLanes = (lanes: Aggregate["lanes"]): Aggregate => ({ ...base, lanes });

describe("laneLayers", () => {
  test("leaves out lanes that recorded nothing", () => {
    const data = withLanes([
      { lane: "git", events: [], phases: [{ kind: "git_conflicting", share: [0, 0, null, 0], count: [0, 0, 0, 0] }] },
      { lane: "slack", events: [{ kind: "slack_post", times: [] }], phases: [] },
    ]);

    expect(laneLayers(data, catalog)).toEqual([]);
  });

  test("stacks a lane's spans over the rest of the tasks in flight", () => {
    const data = withLanes([
      { lane: "git", events: [], phases: [{ kind: "git_conflicting", share: [0.5, 0.25, null, 0], count: [1, 1, 0, 0] }] },
    ]);

    const [layer] = laneLayers(data, catalog);

    expect(layer?.label).toBe("Git");
    expect(layer?.styles.map((s) => s.key)).toEqual(["git_conflicting", NO_ACTIVITY]);
    expect(layer?.styles[0]?.label).toBe(catalog.lane_phases.git_conflicting.label);
    // inflight is [2, 4, 0, 10] in the fixture.
    expect(layer?.shares[1]).toEqual([0.5, 0.75, null, 1]);
    expect(layer?.counts[1]).toEqual([1, 3, 0, 10]);
  });

  test("counts a lane's markers in the window", () => {
    const data = withLanes([
      { lane: "slack", events: [{ kind: "slack_post", times: ["2026-10-05T09:00:00Z", "2026-10-06T09:00:00Z"] }], phases: [] },
    ]);

    const [layer] = laneLayers(data, catalog);

    expect(layer?.events).toEqual([{ kind: "slack_post", label: catalog.lane_events.slack_post.label, count: 2 }]);
  });
});
