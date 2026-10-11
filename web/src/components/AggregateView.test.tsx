import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { Aggregate } from "../api/generated";
import { aggregate } from "../testing/aggregateFixtures";
import { branch, noPr, NOW, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
});

function serve(data: Aggregate | ((weeks: number) => Aggregate) | Error) {
  const fetchMock = vi.fn(async (url: string) => {
    if (data instanceof Error) return { ok: false, status: 502, json: async () => ({}) };
    const weeks = Number(new URL(url, "http://x").searchParams.get("weeks"));
    return { ok: true, status: 200, json: async () => (typeof data === "function" ? data(weeks) : data) };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function show(search = "?view=aggregate", branches = [branch(1, "devon/a", { status: noPr() })]) {
  return renderWith(<App clock={() => NOW} />, snapshot(branches), { search });
}

describe("the aggregate view", () => {
  test("fetches the default window and draws every chart with its legend", async () => {
    const fetchMock = serve(aggregate());
    show();

    expect(screen.getByText(/Loading the last 12 weeks/)).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Throughput" });

    expect(fetchMock).toHaveBeenCalledWith("/api/aggregate?weeks=12", expect.anything());
    for (const name of ["Throughput", "Lead time", "Status mix", "Who is it waiting on"]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
    // The status legend carries the catalog's labels (as served), and the
    // coarse one the grouped view's section names.
    const mix = screen.getByRole("region", { name: "Status mix" });
    expect(within(mix).getByText("Waiting for review")).toBeInTheDocument();
    expect(within(mix).getByText("CI failing")).toBeInTheDocument();
    const who = screen.getByRole("region", { name: "Who is it waiting on" });
    expect(within(who).getByText("Needs me")).toBeInTheDocument();
    expect(within(who).getByText("Waiting on CI")).toBeInTheDocument();
    expect(within(who).getByText("Waiting on others / team")).toBeInTheDocument();
  });

  test("the window control refetches and the URL follows", async () => {
    const fetchMock = serve((weeks) => aggregate({ weeks }));
    const { url } = show("?view=aggregate");
    await screen.findByRole("heading", { name: "Throughput" });
    expect(screen.getByRole("button", { name: "12 weeks" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "26 weeks" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/aggregate?weeks=26", expect.anything()));
    expect(screen.getByRole("button", { name: "26 weeks" })).toHaveAttribute("aria-pressed", "true");
    expect(url.location.search).toBe("?view=aggregate&weeks=26");
    await screen.findByRole("heading", { name: "Throughput" });
  });

  test("a weeks parameter in the URL selects the window", async () => {
    const fetchMock = serve(aggregate({ weeks: 4 }));
    show("?view=aggregate&weeks=4");

    await screen.findByRole("heading", { name: "Throughput" });

    expect(fetchMock).toHaveBeenCalledWith("/api/aggregate?weeks=4", expect.anything());
  });

  test("the view toggle switches to it, and the stack filters give way to the window", async () => {
    serve(aggregate());
    const { url } = show("");
    expect(screen.getByLabelText("Filter")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Aggregate" }));

    await screen.findByRole("heading", { name: "Throughput" });
    expect(url.location.search).toBe("?view=aggregate");
    expect(screen.queryByLabelText("Filter")).not.toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Window" })).toBeInTheDocument();
  });

  test("works with no tracked stacks: the history is what is shown", async () => {
    serve(aggregate());
    show("?view=aggregate", []);

    await screen.findByRole("heading", { name: "Throughput" });
  });

  test("the status mix has an exact-values tooltip, by keyboard", async () => {
    serve(aggregate());
    show();
    const mix = await screen.findByRole("region", { name: "Status mix" });
    const chart = within(mix).getByRole("img");

    chart.focus();
    fireEvent.keyDown(chart, { key: "ArrowRight" });

    const tip = within(mix).getByRole("status");
    expect(tip).toHaveTextContent("Oct 5");
    expect(tip).toHaveTextContent("1.0 tasks in flight on average");
    expect(within(tip).getByText("In draft").nextSibling).toHaveTextContent("50%");

    fireEvent.keyDown(chart, { key: "ArrowRight" });
    fireEvent.keyDown(chart, { key: "ArrowRight" });
    expect(within(mix).getByRole("status")).toHaveTextContent("nothing in flight");
    fireEvent.keyDown(chart, { key: "Escape" });
    expect(within(mix).queryByRole("status")).not.toBeInTheDocument();
  });

  test("every chart summarizes itself for assistive technology", async () => {
    serve(aggregate());
    show();
    const throughput = await screen.findByRole("region", { name: "Throughput" });

    expect(within(throughput).getByRole("img")).toHaveAccessibleName(
      "9 PRs opened and 6 merged over 3 weeks, from the week of Sep 21.",
    );
    const mix = screen.getByRole("region", { name: "Status mix" });
    expect(within(mix).getByRole("img").getAttribute("aria-label")).toMatch(/Latest: .*Approved 40%/);
  });

  test("the data table toggle shows exact values", async () => {
    serve(aggregate());
    show();
    const throughput = await screen.findByRole("region", { name: "Throughput" });
    expect(within(throughput).queryByRole("table")).not.toBeInTheDocument();

    fireEvent.click(within(throughput).getByRole("button", { name: "Show data table" }));

    const table = within(throughput).getByRole("table");
    const rows = within(table).getAllByRole("row").map((row) => row.textContent);
    expect(rows).toEqual([
      "Week ofOpenedMergedClosed unmerged",
      "Sep 21210",
      "Sep 28431",
      "Oct 5320",
    ]);
    fireEvent.click(within(throughput).getByRole("button", { name: "Hide data table" }));
    expect(within(throughput).queryByRole("table")).not.toBeInTheDocument();
  });

  test("lead times show their medians and which intervals are unknown", async () => {
    serve(aggregate());
    show();
    const lead = await screen.findByRole("region", { name: "Lead time" });

    const first = within(lead).getByText("Task started to PR opened").parentElement!;
    expect(first).toHaveTextContent("median 5h");
    expect(first).toHaveTextContent("n=3 (3 unknown)");
    // No data: says why, instead of inventing a value.
    const last = within(lead).getByText("Task started to merged").parentElement!;
    expect(last).toHaveTextContent("no data");
    expect(lead).toHaveTextContent("No task start was recorded for these PRs (starts are recorded since Sep 30)");
  });

  test("says when task starts are only recorded from a date inside the window", async () => {
    serve(aggregate());
    show();

    expect(await screen.findByText(/Task starts are only recorded since Sep 30: 3 of 9 PRs/)).toBeInTheDocument();
  });

  test("an empty window says why", async () => {
    const empty = aggregate();
    empty.mix.series = empty.mix.series.map((s) => ({ ...s, share: s.share.map(() => null) }));
    empty.mix.who = empty.mix.who.map((s) => ({ ...s, share: s.share.map(() => null) }));
    empty.coverage.prs = 0;
    serve(empty);
    show();

    await screen.findByRole("heading", { name: "Status mix" });

    expect(screen.getByText(/None of your PRs were updated in the last 12 weeks/)).toBeInTheDocument();
    expect(screen.getAllByText(/No task was in flight in this window/)).toHaveLength(2);
  });

  test("a partial result is flagged", async () => {
    const partial = aggregate();
    partial.coverage.truncated = true;
    partial.coverage.hidden = 2;
    serve(partial);
    show();

    expect(await screen.findByText(/GitHub has more PRs than were fetched/)).toBeInTheDocument();
    expect(screen.getByText(/2 organizations were not authorized/)).toBeInTheDocument();
  });

  test("a failed fetch is reported", async () => {
    serve(new Error("boom"));
    show();

    expect(await screen.findByText(/Could not load the aggregate: \/api\/aggregate: HTTP 502/)).toBeInTheDocument();
  });
});
