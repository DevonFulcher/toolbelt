import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { EstimateBreakdown } from "../api/generated";
import { NOW, branch, pr, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";
import { learned } from "./EstimateDialog";

afterEach(() => vi.unstubAllGlobals());

const at = (days: number) => new Date(NOW + days * 86400_000).toISOString();

function breakdown(): EstimateBreakdown {
  return {
    id: "1:devon/a",
    stage: "wait_review",
    champion: "regression",
    champion_reason: "champion of wait review: 12% lower pinball loss than the baseline on 41 test points",
    models: [
      {
        model: "baseline",
        champion: false,
        known: true,
        finish_p50: at(3),
        finish_p80: at(5),
        level: "reviewer",
        n: 14,
        explanation: "waiting on ann, 2.1 working days in, based on 14 reviews",
        details: { level: "reviewer", n: 14, factors: [{ dim: "reviewer", value: "ann", n: 14, raw: 0.5, shrunk: 0.7 }] },
      },
      {
        model: "regression",
        champion: true,
        known: true,
        finish_p50: at(2),
        finish_p80: at(4),
        level: "fitted",
        n: 90,
        explanation: "regression, trained on 90 rows of this stage",
        details: { contributions: [{ feature: "reviewer_latency", contribution: -0.4 }] },
      },
      {
        model: "hazard",
        champion: false,
        known: false,
        finish_p50: null,
        finish_p80: null,
        level: "unknown",
        n: 3,
        explanation: "unknown: no history for this stage yet (3 rows)",
        details: {},
      },
    ],
    accuracy: [
      { model: "baseline", points: 41, coverage: 0.78, loss: 5000, median_abs_error_seconds: 7200, trained_on: null, fitted_at: null, loss_vs_baseline: 1 },
      { model: "regression", points: 41, coverage: 0.8, loss: 4400, median_abs_error_seconds: 6000, trained_on: 420, fitted_at: at(-1), loss_vs_baseline: 0.88 },
    ],
    path: [{ stage: "merge", share: 0.9, p50_seconds: 3600, p80_seconds: 7200 }],
    parent: {
      task_key: "o/api#7",
      parent_known: true,
      parent_p50: at(2),
      parent_p80: at(3),
      combined_p50: at(2.5),
      combined_p80: at(4),
    },
    factors: [{ dim: "reviewer", value: "ann", n: 14, raw: 0.5, shrunk: 0.7 }],
    similar: [{ task_key: "o/api#3", stage: "wait_review", reviewer: "ann", seconds: 7200, tail_seconds: 3600, ended_at: at(-10) }],
  };
}

describe("the estimate popup", () => {
  test("shows every model side by side with the champion marked, what each learned, the parent wait and similar tasks", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => breakdown() }));
    vi.stubGlobal("fetch", fetchMock);
    const { store } = renderWith(<App clock={() => NOW} />, snapshot([branch(1, "devon/a", { status: pr({ pr_number: 8 }) })]));
    store.getState().extrasChanged({
      branches: [
        {
          id: "1:devon/a",
          lanes: [],
          agent: { has_action: false, trigger: null, reason: null, can_override: false, starting: false, start_error: null, latest_run: null, runs: 0, recent_runs: [], validation: { can_validate: false, validated: false, result: null, opening_pr: false, notice: null } },
          spend: { interactive_usd: 0, interactive_unpriced_calls: 0, autonomous_usd: 0, autonomous_runs: 0 },
          estimate: {
            known: true, stage: "wait_review", finish_p50: at(2), finish_p80: at(4), overdue: false, partial: false,
            level: "fitted", n: 90, explanation: "x", model: "regression",
          },
        },
      ],
      agents_decided_at: null,
      spend_read_at: null,
    });

    fireEvent.click(await screen.findByRole("button", { name: "Models" }));
    const dialog = await screen.findByRole("dialog");

    expect(fetchMock).toHaveBeenCalledWith("/api/estimate?id=1%3Adevon%2Fa");
    await waitFor(() => expect(within(dialog).getByRole("table", { name: "Models" })).toBeInTheDocument());
    const rows = within(within(dialog).getByRole("table", { name: "Models" })).getAllByRole("row");
    expect(rows[1]?.textContent).toContain("baseline");
    expect(rows[2]?.textContent).toContain("★ regression");
    expect(rows[2]?.textContent).toContain("0.88× baseline");
    expect(rows[2]?.textContent).toContain("420 rows");
    expect(rows[3]?.textContent).toContain("unknown");
    expect(within(dialog).getByText(/regression answers the card/)).toBeInTheDocument();
    expect(within(dialog).getByText(/reviewer ann: ×0.70 \(14 samples\)/)).toBeInTheDocument();
    expect(within(dialog).getByText(/reviewer_latency: -0.40 log-seconds/)).toBeInTheDocument();
    const path = within(dialog).getByRole("list", { name: "Stage path" });
    expect(within(path).getByText(/wait for parent o\/api#7/)).toBeInTheDocument();
    expect(within(path).getByText(/merge: 1.0h \(p50\), up to 2.0h \(p80\), in 90% of similar tasks/)).toBeInTheDocument();
    expect(within(dialog).getByRole("list", { name: "Similar tasks" })).toHaveTextContent("o/api#3 (ann)");
  });

  test("what each model learned, as lines", () => {
    const hazard = {
      model: "hazard", champion: false, known: true, finish_p50: null, finish_p80: null, level: "fitted", n: 5, explanation: "",
      details: { hazard: [{ from_hours: 0, hazard: 0.05 }, { from_hours: 4, hazard: 0.4 }], coefficients: [{ feature: "queue_length", coefficient: 0.3 }] },
    };
    expect(learned(hazard)).toEqual(["queue_length: coefficient 0.30", "hazard by hour bin: 0h 0.05, 4h 0.40"]);
    const trees = { ...hazard, model: "trees", details: { importances: [{ feature: "size_log2", importance: 1.2 }] } };
    expect(learned(trees)).toEqual(["size_log2: importance 1.20"]);
  });
});
