// The card timeline's phase rules against the vectors the aggregate view's
// Python replay is checked against too (tests/test_ui_aggregate.py): the two
// implementations must give the same spans for the same events.
import { describe, expect, test } from "vitest";
import type { ActivityKind } from "../api/generated";
import vectors from "../testing/phaseVectors.json";
import { phaseSpans } from "./timeline";
import type { Track } from "./timeline";

const MINUTE = 60_000;

describe("phaseSpans against the shared vectors", () => {
  for (const vector of vectors.cases) {
    test(vector.name, () => {
      const events = vector.events.map((e) => ({
        kind: e.kind as ActivityKind,
        actor: e.actor,
        subject: e.subject,
        detail: e.detail,
        ci: e.ci,
        ms: e.t * MINUTE,
        ciDone: "ciDone" in e && typeof e.ciDone === "number" ? e.ciDone * MINUTE : null,
      }));
      const track = {
        state: vector.state,
        draft: vector.draft,
        review: vector.review,
      } as Pick<Track, "state" | "draft" | "review">;
      const spans = phaseSpans(events, track, vector.start * MINUTE, vector.end * MINUTE)
        .map((s) => [s.kind, s.t0 / MINUTE, s.t1 / MINUTE])
        .sort((a, b) => (a[1] as number) - (b[1] as number));
      expect(spans).toEqual(vector.spans);
    });
  }
});
