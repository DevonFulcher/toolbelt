import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { Spend } from "../api/generated";
import { branch, noPr, NOW, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { spend } from "../testing/spendFixtures";
import { App } from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
});

function serve(data: Spend | ((days: number) => Spend) | Error) {
  const fetchMock = vi.fn(async (url: string) => {
    if (data instanceof Error) return { ok: false, status: 502, json: async () => ({}) };
    const days = Number(new URL(url, "http://x").searchParams.get("days"));
    return { ok: true, status: 200, json: async () => (typeof data === "function" ? data(days) : data) };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function show(search = "?view=spend") {
  return renderWith(<App clock={() => NOW} />, snapshot([branch(1, "devon/a", { status: noPr() })]), { search });
}

describe("the spend view", () => {
  test("asks for the default window and shows a tile per window", async () => {
    const fetchMock = serve(spend());
    show();

    expect(screen.getByText(/Loading the last 7 days of spend/)).toBeInTheDocument();
    await screen.findByText("$61.67");

    expect(fetchMock).toHaveBeenCalledWith("/api/spend?days=7", expect.anything());
    for (const [label, cost] of [
      ["Last 24 hours", "$61.67"],
      ["Last 7 days", "$377.66"],
      ["Last 30 days", "$1,368.70"],
      ["This month (UTC)", "$574.42"],
    ]) {
      // The interactive tiles come first; the autonomous section repeats the labels.
      const tile = screen.getAllByText(label)[0].closest(".spend-tile") as HTMLElement;
      expect(within(tile).getByText(cost)).toBeInTheDocument();
    }
    expect(screen.getByText("1,166 calls · 280.8M tokens")).toBeInTheDocument();
  });

  test("lists spend by model and by task, naming unclaimed and forgotten tasks", async () => {
    serve(spend());
    show();
    await screen.findByText("$61.67");

    const byModel = screen.getByRole("region", { name: "By model" });
    expect(within(byModel).getByText("claude-sonnet-5-5")).toBeInTheDocument();
    expect(within(byModel).getByText("$322.55")).toBeInTheDocument();

    const byTask = screen.getByRole("region", { name: "By task" });
    expect(within(byTask).getByText("api: devon/a")).toBeInTheDocument();
    expect(within(byTask).getByText("Task gone-tas (no longer tracked)")).toBeInTheDocument();
    expect(within(byTask).getByText("Unclaimed")).toBeInTheDocument();
    expect(within(byTask).getByText("$333.90")).toBeInTheDocument();
  });

  test("the window buttons refetch the breakdown", async () => {
    const fetchMock = serve((days) => spend({ breakdown_days: days }));
    show();
    await screen.findByText("$61.67");

    fireEvent.click(screen.getByRole("button", { name: "30 days" }));

    await screen.findByRole("heading", { name: "By model, last 30 days" });
    expect(fetchMock).toHaveBeenCalledWith("/api/spend?days=30", expect.anything());
  });

  test("autonomous agents' spend is its own section, from the runs' recorded costs", async () => {
    serve(spend());
    show();
    await screen.findByText("$61.67");

    const section = screen.getByRole("region", { name: "Autonomous agents" });
    expect(within(section).getByText("$0.1831")).toBeInTheDocument();
    expect(within(section).getByText("2 runs · 90.0k tokens")).toBeInTheDocument();
    const byTrigger = within(section).getByRole("table", { name: "Autonomous spend by what the run was for" });
    expect(within(byTrigger).getByText("Fix / re-run CI")).toBeInTheDocument();
    expect(within(byTrigger).getByText("$0.2500")).toBeInTheDocument();
    expect(within(section).getByText("api: devon/a")).toBeInTheDocument();
  });

  test("says when no agent has run", async () => {
    const empty = spend();
    serve(spend({ autonomous: { ...empty.autonomous, by_trigger: [], by_task: [] } }));
    show();

    expect(await screen.findByText(/No autonomous run in the last 7 days/)).toBeInTheDocument();
  });

  test("a model with no price is called out and its cost is not shown as zero", async () => {
    serve(
      spend({
        unpriced_models: [{ model: "claude-new-1", calls: 3 }],
        by_model: [
          { model: "claude-new-1", calls: 3, tokens: { input: 1, output: 1, cache_read: 0, cache_write_5m: 0, cache_write_1h: 0 }, cost_usd: 0, unpriced_calls: 3 },
        ],
      }),
    );
    show();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("No price for claude-new-1 (3 calls)");
    expect(alert).toHaveTextContent("uv lock --upgrade-package genai-prices");
    const row = screen.getByText("claude-new-1", { selector: "td" }).closest("tr") as HTMLElement;
    expect(within(row).getByText("no price")).toBeInTheDocument();
    expect(within(row).getByText("–")).toBeInTheDocument();
  });

  test("says so when no claims have been recorded, and before the first read", async () => {
    serve(spend({ claims_since: null, ingested_at: null }));
    show();

    expect(await screen.findByText(/Reading the session transcripts for the first time/)).toBeInTheDocument();
    expect(screen.getByText(/No claims have been recorded yet/)).toBeInTheDocument();
  });

  test("shows the server's error", async () => {
    serve(new Error("down"));
    show();

    expect(await screen.findByText(/Could not load the spend: \/api\/spend: HTTP 502/)).toBeInTheDocument();
  });

  test("is reachable from the view selector and keeps the URL in step", async () => {
    serve(spend());
    const { url } = renderWith(
      <App clock={() => NOW} />,
      snapshot([branch(1, "devon/a", { status: noPr() })]),
      { search: "" },
    );

    fireEvent.click(screen.getByRole("button", { name: "Spend" }));
    await screen.findByText("$61.67");

    await waitFor(() => expect(url.location.search).toBe("?view=spend"));
  });
});
