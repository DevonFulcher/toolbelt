// The aggregate view: how work has been flowing over a window. Fetches
// `/api/aggregate` when opened (and when the window changes); it is not part
// of the live stream.
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchAggregate } from "../api/client";
import type { Aggregate, MixWho, Tone } from "../api/generated";
import { formatBucketDate } from "../logic/aggregateChart";
import { NEXT_SECTIONS } from "../logic/layout";
import { useApp } from "../store/StoreContext";
import { LeadTimeChart, MixChart, ThroughputChart, withHatches } from "./AggregateCharts";

type Load =
  | { weeks: number; state: "ready"; data: Aggregate }
  | { weeks: number; state: "error"; message: string };

/** A fetch older than this is repeated when the page becomes visible again. */
export const STALE_AFTER_MS = 60_000;

export interface AggregateHandle {
  /** The aggregate for `weeks`, or `null` while it is first being fetched. */
  load: Load | null;
  /** Fetch again now (the previous result stays shown meanwhile). */
  refresh: () => void;
  refreshing: boolean;
}

/**
 * The aggregate for `weeks`. The server keeps its answer fresh on every
 * request (there is no cache to wait out), so this fetches when mounted and
 * when the window changes, again when the page becomes visible after more
 * than `STALE_AFTER_MS`, and on `refresh()`.
 */
export function useAggregate(weeks: number, fetchImpl: typeof fetch = fetch): AggregateHandle {
  const [load, setLoad] = useState<Load | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const fetchedAt = useRef(0);
  const latest = useRef(0);
  const run = useCallback(() => {
    const id = ++latest.current;
    setRefreshing(true);
    fetchAggregate(weeks, fetchImpl).then(
      (data) => {
        if (id !== latest.current) return;
        fetchedAt.current = Date.now();
        setLoad({ weeks, state: "ready", data });
        setRefreshing(false);
      },
      (error: unknown) => {
        if (id !== latest.current) return;
        setLoad({ weeks, state: "error", message: error instanceof Error ? error.message : String(error) });
        setRefreshing(false);
      },
    );
  }, [weeks, fetchImpl]);
  // A result still in flight for another window (or after unmount) is dropped.
  const abandon = useCallback(() => {
    latest.current++;
  }, []);
  useEffect(() => {
    run();
    return abandon;
  }, [run, abandon]);
  useEffect(() => {
    const onVisible = (): void => {
      if (document.visibilityState === "visible" && Date.now() - fetchedAt.current > STALE_AFTER_MS) run();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [run]);
  // A result for another window is stale: say "loading" until this one's in.
  return { load: load && load.weeks === weeks ? load : null, refresh: run, refreshing };
}

/** The coarse classes: the sections of the grouped view, so the legend reads
 * as the cards do. */
const WHO_TONE: Record<MixWho, Tone> = { you: "accent", ci: "amber", others: "gray" };

export function AggregateView({ fetchImpl = fetch }: { fetchImpl?: typeof fetch }) {
  const weeks = useApp((s) => s.view.weeks);
  const { load, refresh, refreshing } = useAggregate(weeks, fetchImpl);
  if (load === null) {
    return <div className="empty">{`Loading the last ${weeks} weeks…`}</div>;
  }
  if (load.state === "error") {
    return <div className="empty error">{`Could not load the aggregate: ${load.message}`}</div>;
  }
  return <AggregatePanels data={load.data} onRefresh={refresh} refreshing={refreshing} />;
}

export function AggregatePanels({
  data,
  onRefresh,
  refreshing = false,
}: {
  data: Aggregate;
  onRefresh?: () => void;
  refreshing?: boolean;
}) {
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
        {`. Fetched ${new Date(data.fetched_at).toLocaleTimeString()}, ${data.refreshed} PR${data.refreshed === 1 ? "" : "s"} refreshed from GitHub. `}
        {onRefresh && (
          <button type="button" className="agg-refresh" disabled={refreshing} onClick={onRefresh}>
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        )}
      </p>
      <div className="agg-notices">
        {coverage.prs === 0 && (
          <div className="notice">{`None of your PRs were updated in the last ${data.weeks} weeks.`}</div>
        )}
        {startsAfterWindow && coverage.prs > 0 && (
          <div className="notice">
            {`Task starts are only recorded since ${formatBucketDate(coverage.task_starts_since)}: ${coverage.prs_with_start} of ${coverage.prs} PRs have one. Earlier PRs begin when they were opened, and their "task started" intervals are unknown rather than guessed.`}
          </div>
        )}
        {coverage.starts_by_name > 0 && (
          <div className="notice">
            {`${coverage.starts_by_name} of ${coverage.prs_with_start} task starts matched by branch name (their PR was never recorded, so a reused branch name could be mismatched). The rest are tied to their PR by id.`}
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
      <MixChart
        title="Status mix"
        description={`A snapshot, not an average: at each instant (every ${mix.step_hours} hours) every task in flight has exactly one state, and each line is the share of those tasks in that state, so the lines sum to 100%. Gaps are instants with nothing in flight. Merge conflicts and unresolved comments have no history, so they are not separated.`}
        series={stateSeries}
        shares={mix.series.map((s) => s.share)}
        counts={mix.series.map((s) => s.count)}
        starts={mix.starts}
        inflight={mix.inflight}
        empty={`No task was in flight in this window${startsAfterWindow ? ` (task starts are recorded since ${formatBucketDate(coverage.task_starts_since)})` : ""}.`}
      />
      <MixChart
        title="Who is it waiting on"
        description="The same snapshots, grouped by who the next action waits on: you (open, publish, fix, address review, merge), CI, or others (a reviewer, or a parent PR that waits on one). Derived with the rules of the Next column."
        series={whoSeries}
        shares={mix.who.map((s) => s.share)}
        counts={mix.who.map((s) => s.count)}
        starts={mix.starts}
        inflight={mix.inflight}
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
