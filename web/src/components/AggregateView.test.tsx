import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { Aggregate } from "../api/generated";
import { aggregate } from "../testing/aggregateFixtures";
import { branch, noPr, NOW, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
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
  test("a page refresh shows the saved short-window result while the server request is pending", () => {
    sessionStorage.setItem("toolbelt:aggregate:v3:4:", JSON.stringify(aggregate({ weeks: 4 })));
    const fetchMock = vi.fn(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    show("?view=aggregate&weeks=4");

    expect(screen.getByRole("heading", { name: "Status over time" })).toBeInTheDocument();
    expect(screen.queryByText(/Loading the last 4 weeks/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/aggregate?weeks=4", expect.anything());
  });

  test("fetches the default window and draws every chart with its legend", async () => {
    const fetchMock = serve(aggregate());
    show();

    expect(screen.getByText(/Loading the last 12 weeks/)).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Throughput" });

    expect(fetchMock).toHaveBeenCalledWith("/api/aggregate?weeks=12", expect.anything());
    for (const name of ["Throughput", "Lines changed", "Lead time over time (All time)", "Status over time", "Who is it waiting on"]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
    // The status legend carries the catalog's labels (as served), and the
    // coarse one the grouped view's section names.
    const mix = screen.getByRole("region", { name: "Status over time" });
    expect(within(mix).getByText("Waiting for review")).toBeInTheDocument();
    expect(within(mix).getByText("Fix / re-run CI")).toBeInTheDocument();
    const who = screen.getByRole("region", { name: "Who is it waiting on" });
    expect(within(who).getByText("Needs me")).toBeInTheDocument();
    expect(within(who).getByText("Waiting on CI")).toBeInTheDocument();
    expect(within(who).getByText("Waiting on others / team")).toBeInTheDocument();
  });

  test("draws a chart for each lane that recorded something, with its markers counted", async () => {
    serve(
      aggregate({
        lanes: [
          { lane: "git", events: [], phases: [{ kind: "git_conflicting", share: [0.5, 0.25, null, 0], count: [1, 1, 0, 0] }] },
          { lane: "slack", events: [{ kind: "slack_post", times: ["2026-10-05T09:00:00Z"] }], phases: [] },
          { lane: "agent", events: [], phases: [{ kind: "agent_running", share: [0, 0, null, 0], count: [0, 0, 0, 0] }] },
        ],
      }),
    );
    show();

    const git = await screen.findByRole("region", { name: "Lane: Git" });
    expect(within(git).getByText("Conflicting")).toBeInTheDocument();
    expect(within(git).getByText("No git activity")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Lane: Slack" })).toBeInTheDocument();
    expect(screen.getByText("1 × posted in Slack in this window.")).toBeInTheDocument();
    // A lane with nothing recorded is not drawn.
    expect(screen.queryByRole("region", { name: "Lane: Agent runs" })).not.toBeInTheDocument();
  });

  test("each chart legend describes its properties on hover and focus", async () => {
    serve(aggregate());
    show();
    const throughput = await screen.findByRole("region", { name: "Throughput" });
    const opened = within(throughput).getByText("PRs opened").closest("li")!;
    const openedHelp = within(opened).getByRole("tooltip", { hidden: true });
    expect(openedHelp).toHaveTextContent("PRs opened in each local day or week.");
    expect(opened).toHaveAttribute("aria-describedby", openedHelp.id);

    const lead = screen.getByRole("region", { name: "Lead time over time (All time)" });
    const leadButton = within(lead).getByRole("button", { name: "Task started to PR opened" });
    const leadHelp = within(leadButton.closest("li")!).getByRole("tooltip", { hidden: true });
    expect(leadHelp).toHaveTextContent("Time from the recorded task start until its PR was opened.");
    expect(leadButton).toHaveAttribute("aria-describedby", leadHelp.id);

    const mix = screen.getByRole("region", { name: "Status over time" });
    expect(within(within(mix).getByText("Waiting for review").closest("li")!).getByRole("tooltip", { hidden: true }))
      .toHaveTextContent("Waiting for a reviewer.");
    const who = screen.getByRole("region", { name: "Who is it waiting on" });
    expect(within(within(who).getByText("Needs me").closest("li")!).getByRole("tooltip", { hidden: true }))
      .toHaveTextContent("Your move: fix, resolve, publish, request review or merge.");
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
    expect(fetchMock).toHaveBeenLastCalledWith("/api/aggregate?weeks=12&refresh=true", expect.anything());
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

  test("the status chart has an exact-values tooltip, by keyboard", async () => {
    serve(aggregate());
    show();
    const mix = await screen.findByRole("region", { name: "Status over time" });
    const chart = within(mix).getByRole("img");

    chart.focus();
    fireEvent.keyDown(chart, { key: "ArrowRight" });

    const tip = within(mix).getByRole("status");
    expect(tip).toHaveTextContent("Mon Oct 5, 00:00");
    expect(tip).toHaveTextContent("2 tasks in flight");
    expect(within(tip).getByText("Publish PR in GitHub").nextSibling).toHaveTextContent("50% (1 of 2)");

    fireEvent.keyDown(chart, { key: "ArrowRight" });
    expect(within(mix).getByRole("status")).toHaveTextContent("Mon Oct 5, 06:00");
    fireEvent.keyDown(chart, { key: "ArrowRight" });
    expect(within(mix).getByRole("status")).toHaveTextContent("nothing in flight");
    fireEvent.keyDown(chart, { key: "Escape" });
    expect(within(mix).queryByRole("status")).not.toBeInTheDocument();
  });

  test("the 12-week status chart keeps its stacked chart and marks exact transition times", async () => {
    serve(aggregate());
    show();
    const mix = await screen.findByRole("region", { name: "Status over time" });

    expect(mix.querySelectorAll("path.agg-band").length).toBeGreaterThan(0);
    expect(mix.querySelectorAll("path.agg-line")).toHaveLength(0);
    expect(mix.querySelectorAll("line.agg-transition-tick")).toHaveLength(2);
    // The separate tasks-in-flight strip is gone: the number scale shows it.
    expect(mix.querySelectorAll("rect.agg-inflight")).toHaveLength(0);
  });

  test("the mix charts show shares by default and can switch to the number of tasks", async () => {
    serve(aggregate());
    show();
    const mix = await screen.findByRole("region", { name: "Status over time" });
    const ticks = (): string[] => [...mix.querySelectorAll("text.agg-tick")].map((t) => t.textContent ?? "");

    expect(within(mix).getByRole("button", { name: "Share of tasks" })).toHaveAttribute("aria-pressed", "true");
    expect(ticks()).toEqual(expect.arrayContaining(["0%", "50%", "100%"]));
    expect(mix).toHaveTextContent("share of tasks in flight");

    fireEvent.click(within(mix).getByRole("button", { name: "Number of tasks" }));

    expect(within(mix).getByRole("button", { name: "Number of tasks" })).toHaveAttribute("aria-pressed", "true");
    // The most in flight in the fixture is 10: the axis runs to a nice number above it.
    expect(ticks()).not.toContain("100%");
    expect(ticks()).toEqual(expect.arrayContaining(["0", "10"]));
    expect(mix).toHaveTextContent("tasks in flight");
    expect(mix.querySelectorAll("path.agg-band").length).toBeGreaterThan(0);

    fireEvent.click(within(mix).getByRole("button", { name: "Share of tasks" }));
    expect(ticks()).toContain("100%");
  });

  test("the who-is-it-waiting-on chart has the same switch", async () => {
    serve(aggregate());
    show();
    const who = await screen.findByRole("region", { name: "Who is it waiting on" });

    fireEvent.click(within(who).getByRole("button", { name: "Number of tasks" }));

    expect(within(who).getByRole("button", { name: "Number of tasks" })).toHaveAttribute("aria-pressed", "true");
    expect(who.querySelectorAll("path.agg-band").length).toBeGreaterThan(0);
  });

  test("who the work is waiting on keeps its stacked chart and exact transition times", async () => {
    serve(aggregate());
    show();
    const who = await screen.findByRole("region", { name: "Who is it waiting on" });

    expect(who.querySelectorAll("path.agg-band").length).toBeGreaterThan(0);
    expect(who.querySelectorAll("path.agg-line")).toHaveLength(0);
    expect(who.querySelectorAll("line.agg-transition-tick")).toHaveLength(2);
  });

  test("the 26-week status chart keeps its area view", async () => {
    serve(aggregate({ weeks: 26 }));
    show("?view=aggregate&weeks=26");
    const mix = await screen.findByRole("region", { name: "Status over time" });
    expect(mix.querySelectorAll("path.agg-band").length).toBeGreaterThan(0);
    expect(mix.querySelectorAll("path.agg-line")).toHaveLength(0);
    expect(mix.querySelectorAll("line.agg-transition-tick")).toHaveLength(0);
  });

  test("weekends on the stacked charts are only a thin strip on the axis, described on hover", async () => {
    // Samples spanning Fri Oct 9 to Mon Oct 12, 2026: Saturday and Sunday are in range.
    const base = aggregate();
    serve(
      aggregate({
        samples: {
          ...base.samples,
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
    for (const name of ["Status over time", "Who is it waiting on"]) {
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

  test("each chart can collapse weekends independently", async () => {
    const base = aggregate();
    serve(aggregate({
      samples: { ...base.samples, starts: [
        "2026-10-09T18:00:00Z", "2026-10-10T12:00:00Z",
        "2026-10-11T12:00:00Z", "2026-10-12T18:00:00Z",
      ] },
    }));
    show();
    const flow = await screen.findByRole("region", { name: "Throughput" });
    const lead = screen.getByRole("region", { name: "Lead time over time (All time)" });
    const mix = screen.getByRole("region", { name: "Status over time" });
    const who = screen.getByRole("region", { name: "Who is it waiting on" });

    const bars = flow.querySelectorAll("g.agg-col").length;
    fireEvent.click(within(flow).getByRole("checkbox", { name: "Exclude weekends" }));
    expect(flow.querySelectorAll("g.agg-col").length).toBeLessThan(bars);
    expect(flow.querySelectorAll("rect.agg-weekend")).toHaveLength(0);
    expect(mix.querySelectorAll("rect.agg-weekend-strip").length).toBeGreaterThan(0);

    const before = mix.querySelector("path.agg-band")!.getAttribute("d");
    fireEvent.click(within(mix).getByRole("checkbox", { name: "Exclude weekends" }));
    expect(mix.querySelector("path.agg-band")!.getAttribute("d")).not.toBe(before);
    expect(mix.querySelectorAll("rect.agg-weekend-strip")).toHaveLength(0);
    expect(who.querySelectorAll("rect.agg-weekend-strip").length).toBeGreaterThan(0);

    fireEvent.click(within(who).getByRole("checkbox", { name: "Exclude weekends" }));
    expect(who.querySelectorAll("rect.agg-weekend-strip")).toHaveLength(0);
    fireEvent.click(within(lead).getByRole("button", { name: "Excluding weekends" }));
    expect(lead.querySelectorAll("rect.agg-weekend")).toHaveLength(0);
  });

  test("few tasks in flight are called out as coarse, and the table gives counts", async () => {
    serve(aggregate());
    show();
    const mix = await screen.findByRole("region", { name: "Status over time" });

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
    const mix = screen.getByRole("region", { name: "Status over time" });
    expect(within(mix).getByRole("img").getAttribute("aria-label")).toMatch(/\(10 tasks\): .*Merge 40%/);
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
    // The bar chart has an accessible summary naming the mode and the bucket.
    expect(within(lead).getByRole("img").getAttribute("aria-label")).toMatch(
      /^Lead time \(All time\), Jul 20 to Oct 8, median per day by merge date:/,
    );
  });

  test("lead times are grouped bars, one bar per step in each day, not points and lines", async () => {
    serve(aggregate());
    show();
    const lead = await screen.findByRole("region", { name: "Lead time over time (All time)" });

    expect(lead.querySelectorAll("g.agg-col rect.agg-bar").length).toBeGreaterThan(0);
    expect(lead.querySelectorAll("path.agg-line")).toHaveLength(0);
    expect(lead.querySelectorAll("circle.agg-dot")).toHaveLength(0);
    expect(lead).toHaveTextContent("Each group of bars is the PRs that merged in one day");
    expect(within(lead).queryByRole("checkbox", { name: /p75 to p90/ })).not.toBeInTheDocument();
  });

  test("lines added and deleted by merged PRs are charted next to throughput", async () => {
    const base = aggregate();
    const merged = (number: number, ended: string, additions: number, deletions: number) => ({
      ...base.prs[0],
      number,
      state: "MERGED" as const,
      ended,
      additions,
      deletions,
    });
    serve(
      aggregate({
        prs: [merged(101, "2026-10-06T15:00:00Z", 100, 10), merged(102, "2026-10-07T15:00:00Z", 50, 5)],
      }),
    );
    show();
    const lines = await screen.findByRole("region", { name: "Lines changed" });

    const legend = within(lines).getByRole("list");
    expect(within(legend).getByText("Lines added").parentElement).toHaveTextContent("150 total");
    expect(within(legend).getByText("Lines deleted").parentElement).toHaveTextContent("15 total");
    expect(lines.querySelectorAll("g.agg-col rect").length).toBeGreaterThan(0);
    fireEvent.click(within(lines).getByRole("button", { name: "Show data table" }));
    expect(within(within(lines).getByRole("table")).getByText("100")).toBeInTheDocument();
  });

  test("a step can be hidden from the legend so the total reads on its own", async () => {
    serve(aggregate());
    show();
    const lead = await screen.findByRole("region", { name: "Lead time over time (All time)" });
    const steps = lead.querySelectorAll("rect.agg-bar");
    expect(steps.length).toBeGreaterThan(1);

    fireEvent.click(within(lead).getByRole("button", { name: "Task started to PR opened" }));

    expect(lead.querySelectorAll("rect.agg-bar").length).toBeLessThan(steps.length);
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
    empty.samples.inflight = empty.samples.inflight.map(() => 0);
    empty.lanes = empty.lanes.map((lane) => ({
      ...lane,
      phases: lane.phases.map((p) => ({ ...p, share: p.share.map(() => null), count: p.count.map(() => 0) })),
    }));
    empty.waiting_on = empty.waiting_on.map((s) => ({ ...s, share: s.share.map(() => null), count: s.count.map(() => 0) }));
    empty.prs = [];
    serve(empty);
    show();

    await screen.findByRole("heading", { name: "Status over time" });

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

  test("says when the GitHub history was last refreshed, since it is only refreshed every 15 minutes", async () => {
    serve(aggregate());
    show();

    expect(await screen.findByText(/GitHub history last refreshed .* about every 15 minutes/)).toBeInTheDocument();
  });
});
