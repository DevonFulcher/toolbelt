// The aggregate view: how work has been flowing over a window. Fetches
// `/api/aggregate` when opened (and when the window changes); it is not part
// of the live stream.
import { useEffect, useState } from "react";
import { fetchAggregate } from "../api/client";
import type { Aggregate, MixWho, Tone } from "../api/generated";
import { formatBucketDate } from "../logic/aggregateChart";
import { NEXT_SECTIONS } from "../logic/layout";
import { useApp } from "../store/StoreContext";
import { LeadTimeChart, ShareChart, ThroughputChart, withHatches } from "./AggregateCharts";

type Load =
  | { weeks: number; state: "ready"; data: Aggregate }
  | { weeks: number; state: "error"; message: string };

/** The aggregate for `weeks`, or `null` while it is being fetched. */
export function useAggregate(weeks: number, fetchImpl: typeof fetch = fetch): Load | null {
  const [load, setLoad] = useState<Load | null>(null);
  useEffect(() => {
    let current = true;
    fetchAggregate(weeks, fetchImpl).then(
      (data) => current && setLoad({ weeks, state: "ready", data }),
      (error: unknown) =>
        current &&
        setLoad({ weeks, state: "error", message: error instanceof Error ? error.message : String(error) }),
    );
    return () => {
      current = false;
    };
  }, [weeks, fetchImpl]);
  // A result for another window is stale: say "loading" until this one's in.
  return load && load.weeks === weeks ? load : null;
}

/** The coarse classes: the sections of the grouped view, so the legend reads
 * as the cards do. */
const WHO_TONE: Record<MixWho, Tone> = { you: "accent", ci: "amber", others: "gray" };

export function AggregateView({ fetchImpl = fetch }: { fetchImpl?: typeof fetch }) {
  const weeks = useApp((s) => s.view.weeks);
  const load = useAggregate(weeks, fetchImpl);
  if (load === null) {
    return <div className="empty">{`Loading the last ${weeks} weeks…`}</div>;
  }
  if (load.state === "error") {
    return <div className="empty error">{`Could not load the aggregate: ${load.message}`}</div>;
  }
  return <AggregatePanels data={load.data} />;
}

export function AggregatePanels({ data }: { data: Aggregate }) {
  const catalog = useApp((s) => s.catalog);
  const { throughput, mix, coverage } = data;
  const opened = catalog?.events.opened;
  const merged = catalog?.events.merged;

  const stateSeries = withHatches(
    mix.states.map((s) => ({ key: s.key, label: s.label, tone: s.tone, legend: s.legend })),
  );
  const whoSeries = withHatches(
    mix.who.map((w) => {
      const section = NEXT_SECTIONS.find((s) => s.key === w.key);
      return {
        key: w.key,
        label: section?.label ?? w.key,
        tone: WHO_TONE[w.key],
        legend: section?.meaning ?? "",
      };
    }),
  );
  const throughputStyles = withHatches([
    { key: "opened", label: "PRs opened", tone: opened?.tone ?? "green", legend: opened?.legend ?? "" },
    { key: "merged", label: "PRs merged", tone: merged?.tone ?? "purple", legend: merged?.legend ?? "" },
  ]);
  const mergedPrs = throughput.merged.reduce((a, b) => a + b, 0);
  const startsAfterWindow = Date.parse(coverage.task_starts_since) > Date.parse(data.window_start);

  return (
    <section className="aggregate" aria-label="Aggregate">
      <p className="agg-summary">
        {`${formatBucketDate(data.window_start)} to ${formatBucketDate(data.window_end)} (${data.weeks} weeks): ${coverage.prs} of your PRs`}
        {coverage.tasks_without_pr > 0 && `, ${coverage.tasks_without_pr} task${coverage.tasks_without_pr === 1 ? "" : "s"} without a PR yet`}
        {`. GitHub history fetched ${new Date(data.fetched_at).toLocaleTimeString()} (cached for a few minutes).`}
      </p>
      <div className="agg-notices">
        {coverage.prs === 0 && (
          <div className="notice">{`None of your PRs were updated in the last ${data.weeks} weeks, so there is nothing to chart but tasks still being worked on.`}</div>
        )}
        {startsAfterWindow && (
          <div className="notice">
            {`Task starts are only recorded since ${formatBucketDate(coverage.task_starts_since)}: ${coverage.prs_with_start} of ${coverage.prs} PRs have one. Earlier PRs begin when they were opened, and their "task started" intervals are unknown rather than guessed.`}
          </div>
        )}
        {coverage.truncated && (
          <div className="notice">GitHub has more PRs than were fetched: older ones in this window may be missing.</div>
        )}
        {coverage.hidden > 0 && (
          <div className="notice">{`${coverage.hidden} organization${coverage.hidden === 1 ? " was" : "s were"} not authorized for this token: their PRs are missing.`}</div>
        )}
      </div>
      <ThroughputChart
        starts={throughput.starts}
        opened={throughput.opened}
        merged={throughput.merged}
        closed={throughput.closed}
        styles={{ opened: throughputStyles[0], merged: throughputStyles[1] }}
      />
      <LeadTimeChart
        stages={data.lead_time}
        mergedPrs={mergedPrs}
        startsSince={coverage.task_starts_since}
        tone="accent"
      />
      <ShareChart
        title="Status mix"
        description="The share of in-flight task time spent in each state, per day (per week beyond 12 weeks). Each column sums to 100%. Merge conflicts and unresolved comments have no history, so they are not separated."
        series={stateSeries}
        shares={mix.series.map((s) => s.share)}
        starts={mix.starts}
        inflight={mix.inflight}
        granularity={mix.granularity}
        empty={`No task was in flight in this window${startsAfterWindow ? ` (task starts are recorded since ${formatBucketDate(coverage.task_starts_since)})` : ""}.`}
      />
      <ShareChart
        title="Who is it waiting on"
        description="The same time, grouped by who the next action waits on: you (open, publish, fix, address review, merge), CI, or others (a reviewer, or a parent PR that waits on one). Derived with the rules of the Next column."
        series={whoSeries}
        shares={mix.who.map((s) => s.share)}
        starts={mix.starts}
        inflight={mix.inflight}
        granularity={mix.granularity}
        empty="No task was in flight in this window."
      />
      <details className="agg-notes">
        <summary>What is approximated or not reconstructable</summary>
        <dl>
          {data.notes.map((note) => (
            <div key={note.label}>
              <dt>{note.label}</dt>
              <dd>{note.why}</dd>
            </div>
          ))}
        </dl>
      </details>
    </section>
  );
}
