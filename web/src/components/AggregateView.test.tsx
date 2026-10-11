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
    for (const name of ["Throughput", "Lead time over time (All time)", "Status mix", "Who is it waiting on"]) {
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

  test("says when it was fetched and how many PRs were refreshed, and has a refresh button", async () => {
    const fetchMock = serve(aggregate());
    show();
    await screen.findByRole("heading", { name: "Throughput" });
    expect(screen.getByText(/4 PRs refreshed from GitHub/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    // The previous result stays up while it refreshes.
    expect(screen.getByRole("heading", { name: "Throughput" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled());
  });

  test("becoming visible again refetches only when the last fetch is older than a minute", async () => {
    const fetchMock = serve(aggregate());
    const clock = vi.spyOn(Date, "now");
    clock.mockReturnValue(1_000_000);
    show();
    await screen.findByRole("heading", { name: "Throughput" });
    const becomeVisible = (): void => {
      Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    };

    clock.mockReturnValue(1_000_000 + 30_000);
    becomeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    clock.mockReturnValue(1_000_000 + 61_000);
    becomeVisible();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    clock.mockRestore();
  });

  test("the repo filter is passed through and the per-repo sample size is prominent", async () => {
    const fetchMock = serve((weeks) => aggregate({ weeks, repo: "demo-app" }));
    const { url } = renderWith(<App clock={() => NOW} />, snapshot([branch(1, "devon/a", { status: noPr() })]), {
      search: "?view=aggregate&repo=1",
    });

    await screen.findByRole("heading", { name: "Throughput" });

    expect(fetchMock).toHaveBeenCalledWith("/api/aggregate?weeks=12&repo=1", expect.anything());
    expect(screen.getByText("demo-app: n = 9 PRs.")).toBeInTheDocument();
    // The stack filters stay hidden for this view; the repo one is shown.
    expect(screen.getByLabelText("Repo")).toBeInTheDocument();
    expect(screen.queryByLabelText("Filter")).not.toBeInTheDocument();
    expect(url.location.search).toContain("repo=1");
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
    expect(tip).toHaveTextContent("Mon Oct 5, 00:00");
    expect(tip).toHaveTextContent("2 tasks in flight");
    expect(within(tip).getByText("In draft").nextSibling).toHaveTextContent("50% (1 of 2)");

    fireEvent.keyDown(chart, { key: "ArrowRight" });
    expect(within(mix).getByRole("status")).toHaveTextContent("Mon Oct 5, 06:00");
    fireEvent.keyDown(chart, { key: "ArrowRight" });
    expect(within(mix).getByRole("status")).toHaveTextContent("nothing in flight");
    fireEvent.keyDown(chart, { key: "Escape" });
    expect(within(mix).queryByRole("status")).not.toBeInTheDocument();
  });

  test("the status mix is a stacked area only: there is no lines mode", async () => {
    serve(aggregate());
    show();
    const mix = await screen.findByRole("region", { name: "Status mix" });

    expect(mix.querySelectorAll("path.agg-band").length).toBeGreaterThan(0);
    expect(mix.querySelectorAll("path.agg-line")).toHaveLength(0);
    expect(within(mix).queryByRole("button", { name: "Lines" })).toBeNull();
    expect(within(mix).queryByRole("button", { name: "Stacked area" })).toBeNull();
    // The tasks-in-flight strip is drawn for the sampled instants with tasks.
    expect(mix.querySelectorAll("rect.agg-inflight")).toHaveLength(3);
  });

  test("who the work is waiting on is a stacked area only too", async () => {
    serve(aggregate());
    show();
    const who = await screen.findByRole("region", { name: "Who is it waiting on" });

    expect(who.querySelectorAll("path.agg-band").length).toBeGreaterThan(0);
    expect(who.querySelectorAll("path.agg-line")).toHaveLength(0);
    expect(within(who).queryByRole("button", { name: "Lines" })).toBeNull();
  });

  test("weekends on the stacked charts are only a thin strip on the axis, described on hover", async () => {
    // Samples spanning Fri Oct 9 to Mon Oct 12, 2026: Saturday and Sunday are in range.
    const base = aggregate();
    serve(
      aggregate({
        mix: {
          ...base.mix,
          starts: [
            "2026-10-09T18:00:00Z",
            "2026-10-10T12:00:00Z",
            "2026-10-11T12:00:00Z",
            "2026-10-12T18:00:00Z",
          ],
        },
      }),
    );
    show();
    for (const name of ["Status mix", "Who is it waiting on"]) {
      const chart = await screen.findByRole("region", { name });
      const svg = chart.querySelector("svg[role=img]")!;
      const strips = [...svg.querySelectorAll("rect.agg-weekend-strip")];

      // No hatch or wash over the stack: the stack stays unobscured.
      expect(svg.querySelectorAll("rect.agg-weekend"), name).toHaveLength(0);
      expect(strips.length, name).toBeGreaterThan(0);
      // Each strip says what it is, with its dates, on hover.
      expect(strips[0].querySelector("title")!.textContent, name).toMatch(/^Weekend: Oct 10 to Oct 11$/);
      // The strip is painted after the hit area so it can receive the hover.
      const painted = [...svg.querySelectorAll("rect.agg-hit, rect.agg-weekend-strip")];
      expect(painted.findIndex((e) => e.classList.contains("agg-weekend-strip")), name).toBeGreaterThan(
        painted.findIndex((e) => e.classList.contains("agg-hit")),
      );
    }
  });

  test("few tasks in flight are called out as coarse, and the table gives counts", async () => {
    serve(aggregate());
    show();
    const mix = await screen.findByRole("region", { name: "Status mix" });

    expect(mix).toHaveTextContent("Typically 4 tasks in flight: the percentages are coarse.");
    fireEvent.click(within(mix).getByRole("button", { name: "Show data table" }));

    const rows = within(within(mix).getByRole("table")).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("Oct 5, 00:00");
    expect(rows[1]).toHaveTextContent("50% (1)");
    expect(rows[3]).toHaveTextContent("Oct 5, 12:00");
  });

  test("every chart summarizes itself for assistive technology", async () => {
    serve(aggregate());
    show();
    const throughput = await screen.findByRole("region", { name: "Throughput" });

    expect(within(throughput).getByRole("img")).toHaveAccessibleName(
      "9 PRs opened and 4 merged over 81 days, from Mon Jul 20.",
    );
    const mix = screen.getByRole("region", { name: "Status mix" });
    expect(within(mix).getByRole("img").getAttribute("aria-label")).toMatch(/\(10 tasks\): .*Approved 40%/);
  });

  test("the data table toggle shows exact values, per local day", async () => {
    serve(aggregate());
    show();
    const throughput = await screen.findByRole("region", { name: "Throughput" });
    expect(within(throughput).queryByRole("table")).not.toBeInTheDocument();

    fireEvent.click(within(throughput).getByRole("button", { name: "Show data table" }));

    const table = within(throughput).getByRole("table");
    const rows = within(table).getAllByRole("row").map((row) => row.textContent);
    expect(rows[0]).toBe("DayWeekendOpenedMergedClosed unmerged");
    expect(rows).toContain("Thu Sep 24100");
    expect(rows).toContain("Fri Sep 25010");
    expect(rows).toContain("Sat Sep 26yes000");
    fireEvent.click(within(throughput).getByRole("button", { name: "Hide data table" }));
    expect(within(throughput).queryByRole("table")).not.toBeInTheDocument();
  });

  test("lead times are medians over time with a summary of each step and what is unknown", async () => {
    serve(aggregate());
    show();
    const lead = await screen.findByRole("region", { name: "Lead time over time (All time)" });
    const summary = within(lead).getByRole("table", { name: "Over the whole window (All time)" });
    const row = (step: string): string =>
      within(summary).getByText(step).closest("tr")!.textContent ?? "";

    // PRs 1 and 2 have a start (5h and 6h before opening); 3 and 4 do not.
    expect(row("Task started to PR opened")).toBe("Task started to PR opened225.5h5.8h5.9h");
    expect(row("Task started to merged")).toMatch(/^Task started to merged221\.9d/);
    // Reviewed: PRs 1 (8h), 2 (24h) and 3 (23h); PR 4 has no review.
    expect(row("Review requested to first review")).toMatch(/^Review requested to first review3123h/);
    // The line chart has an accessible summary naming the mode.
    expect(within(lead).getByRole("img").getAttribute("aria-label")).toMatch(
      /^Lead time \(All time\), Jul 20 to Oct 8, by merge date:/,
    );
  });

  test("a step can be hidden from the legend so the total reads on its own", async () => {
    serve(aggregate());
    show();
    const lead = await screen.findByRole("region", { name: "Lead time over time (All time)" });
    const steps = lead.querySelectorAll("path.agg-line");
    expect(steps.length).toBeGreaterThan(1);

    fireEvent.click(within(lead).getByRole("button", { name: "Task started to PR opened" }));

    expect(lead.querySelectorAll("path.agg-line")).toHaveLength(steps.length - 1);
    expect(within(lead).getByRole("button", { name: "Task started to PR opened" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  test("excluding weekends switches every lead-time number, in the URL and the title", async () => {
    const fetchMock = serve(aggregate());
    const { url } = show();
    const lead = await screen.findByRole("region", { name: "Lead time over time (All time)" });
    const review = (): string =>
      within(within(lead).getByRole("table", { name: /Over the whole window/ }))
        .getByText("Review requested to first review")
        .closest("tr")!.textContent!;
    expect(review()).toContain("23h");

    fireEvent.click(within(lead).getByRole("button", { name: "Excluding weekends" }));

    const excluded = await screen.findByRole("region", { name: "Lead time over time (Excluding weekends)" });
    expect(url.location.search).toBe("?view=aggregate&weekends=exclude");
    // PR 3's review came Saturday morning: Friday's 14 hours remain of its 23.
    expect(
      within(within(excluded).getByRole("table", { name: "Over the whole window (Excluding weekends)" }))
        .getByText("Review requested to first review")
        .closest("tr")!.textContent,
    ).toContain("14h");
    // No refetch: both modes come from the same response.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(within(excluded).getByRole("button", { name: "All time" }));
    await screen.findByRole("region", { name: "Lead time over time (All time)" });
    expect(url.location.search).toBe("?view=aggregate");
  });

  test("the weekends parameter in the URL selects the mode", async () => {
    serve(aggregate());
    show("?view=aggregate&weekends=exclude");

    await screen.findByRole("region", { name: "Lead time over time (Excluding weekends)" });
  });

  test("says when task starts are only recorded from a date inside the window", async () => {
    serve(aggregate());
    show();

    expect(await screen.findByText(/Task starts are only recorded since Sep 30: 3 of 9 PRs/)).toBeInTheDocument();
  });

  test("an empty window says why", async () => {
    const empty = aggregate();
    empty.mix.inflight = empty.mix.inflight.map(() => 0);
    empty.mix.series = empty.mix.series.map((s) => ({ ...s, share: s.share.map(() => null), count: s.count.map(() => 0) }));
    empty.mix.who = empty.mix.who.map((s) => ({ ...s, share: s.share.map(() => null), count: s.count.map(() => 0) }));
    empty.prs = [];
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
