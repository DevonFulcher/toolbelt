import { describe, expect, test } from "vitest";
import type { LiveStatus } from "../api/generated";
import { rollup, youText } from "./rollup";

const loaded = (who: "you" | "agent" | "ci" | "others" | "none", label: string): LiveStatus =>
  ({ load: "loaded", next: { who, label, kind: "merge" } }) as unknown as LiveStatus;

describe("rollup", () => {
  test("counts a card and its descendants by who each waits on, the card included", () => {
    const statuses: Record<string, LiveStatus> = {
      a: loaded("you", "Merge"),
      b: loaded("you", "Resolve comments"),
      c: loaded("you", "Merge"),
      d: loaded("ci", "Waiting on CI"),
      e: loaded("others", "Waiting for review"),
      f: loaded("none", "Done"),
    };

    const result = rollup(["a", "b", "c", "d", "e", "f"], (id) => statuses[id]);

    expect(result.total).toBe(6);
    expect(result.you).toEqual([
      { label: "Merge", n: 2 },
      { label: "Resolve comments", n: 1 },
    ]);
    expect([result.ci, result.others, result.done, result.loading]).toEqual([1, 1, 1, 0]);
  });

  test("branches claimed by an agent are their own bucket", () => {
    const statuses: Record<string, LiveStatus> = {
      a: loaded("agent", "With an agent"),
      b: loaded("agent", "With an agent"),
      c: loaded("you", "Merge"),
    };

    const result = rollup(["a", "b", "c"], (id) => statuses[id]);

    expect(result.agent).toBe(2);
    expect(result.you).toEqual([{ label: "Merge", n: 1 }]);
    expect(result.total).toBe(3);
  });

  test("every branch lands in exactly one bucket, unknown statuses as loading", () => {
    const statuses: Record<string, LiveStatus> = { a: loaded("you", "Open PR"), b: { load: "failed" } };

    const result = rollup(["a", "b", "missing"], (id) => statuses[id]);

    const buckets = result.you.reduce((n, entry) => n + entry.n, 0) + result.agent + result.ci + result.others + result.done + result.loading;
    expect(buckets).toBe(result.total);
    expect(result.loading).toBe(2);
  });

  test("youText lists the labels with a count where one repeats", () => {
    expect(youText([{ label: "Merge", n: 2 }, { label: "Open PR", n: 1 }])).toBe("Merge ×2, Open PR");
    expect(youText([])).toBe("");
  });
});
