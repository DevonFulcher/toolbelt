// The aggregate view: how work has been flowing over a window. Fetches
// `/api/aggregate` when opened (and when the window changes); the server
// refreshes its history in the background.
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchAggregate } from "../api/client";
import type { Aggregate, MixWho, Tone } from "../api/generated";
import { formatBucketDate } from "../logic/aggregateChart";
import { bucketDays, leadTimeSeries, STAGES, type StageKey } from "../logic/leadTime";
import { browserTimeZone, formatLocalDate, windowStart } from "../logic/localTime";
import { linesThroughput, throughput } from "../logic/throughput";
import type { Counts } from "../store/selectors";
import { NEXT_SECTIONS } from "../logic/layout";
import { useApp, useAppStore } from "../store/StoreContext";
import { LeadTimeChart, LinesChart, MixChart, ThroughputChart, withHatches, type LeadStageStyle } from "./AggregateCharts";
import { FilterBar } from "./FilterBar";

type Load =
  | { weeks: number; repo: string; state: "ready"; data: Aggregate }
  | { weeks: number; repo: string; state: "error"; message: string };

const storedKey = (weeks: number, repo: string): string => `toolbelt:aggregate:v1:${weeks}:${repo}`;

function storedAggregate(weeks: number, repo: string): Aggregate | null {
  if (weeks > 12) return null;
  try {
    const value = sessionStorage.getItem(storedKey(weeks, repo));
    return value ? JSON.parse(value) as Aggregate : null;
  } catch {
    return null;
  }
}

function storeAggregate(weeks: number, repo: string, data: Aggregate): void {
  if (weeks > 12) return;
  try {
    sessionStorage.setItem(storedKey(weeks, repo), JSON.stringify(data));
  } catch {
    // Browser storage is optional; the server cache still serves this view.
  }
}

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
 * The aggregate for `weeks`. The server refreshes its cache in the background,
 * so this reads it when mounted and
 * when the window changes, again when the page becomes visible after more
 * than `STALE_AFTER_MS`, and on `refresh()`.
 */
export function useAggregate(
  weeks: number,
  repo: string,
  fetchImpl: typeof fetch = fetch,
): AggregateHandle {
  const [load, setLoad] = useState<Load | null>(() => {
    const data = storedAggregate(weeks, repo);
    return data ? { weeks, repo, state: "ready", data } : null;
  });
  const [refreshing, setRefreshing] = useState(false);
  const fetchedAt = useRef(0);
  const latest = useRef(0);
  const run = useCallback((force = false) => {
    const id = ++latest.current;
    if (!force) {
      const data = storedAggregate(weeks, repo);
      if (data) setLoad({ weeks, repo, state: "ready", data });
    }
    setRefreshing(true);
    fetchAggregate(weeks, repo, fetchImpl, force).then(
      (data) => {
        if (id !== latest.current) return;
        fetchedAt.current = Date.now();
        storeAggregate(weeks, repo, data);
        setLoad({ weeks, repo, state: "ready", data });
        setRefreshing(false);
      },
      (error: unknown) => {
        if (id !== latest.current) return;
        setLoad((previous) => previous?.state === "ready" && previous.weeks === weeks && previous.repo === repo
          ? previous
          : { weeks, repo, state: "error", message: error instanceof Error ? error.message : String(error) });
        setRefreshing(false);
      },
    );
  }, [weeks, repo, fetchImpl]);
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
  const current = load && load.weeks === weeks && load.repo === repo;
  return { load: current ? load : null, refresh: () => run(true), refreshing };
}

/** The coarse classes: the sections of the grouped view, so the legend reads
 * as the cards do. */
const WHO_TONE: Record<MixWho, Tone> = { you: "accent", ci: "amber", others: "gray" };

/** The lead-time steps' lines use the page's existing tones. */
const LEAD_TONES: Record<StageKey, Tone> = {
  start_to_open: "amber",
  open_to_request: "green",
  request_to_review: "purple",
  review_to_merge: "gray",
  start_to_merge: "accent",
};

const LEAD_DESCRIPTIONS: Record<StageKey, string> = {
  start_to_open: "Time from the recorded task start until its PR was opened.",
  open_to_request: "Time from PR opened until review was first requested.",
  request_to_review: "Time from the first review request until the first review after it.",
  review_to_merge: "Time from the PR's first review until it was merged.",
  start_to_merge: "Total time from the recorded task start until the PR was merged.",
};

/**
 * The aggregate view's whole page: the controls (the shared bar, with this
 * view's fetch time and refresh button in its options row) and the charts.
 */
export function AggregatePage({
  counts,
  fetchImpl = fetch,
}: {
  counts: Counts;
  fetchImpl?: typeof fetch;
}) {
  const weeks = useApp((s) => s.view.weeks);
  const repo = useApp((s) => s.view.repo);
  const weekends = useApp((s) => s.view.weekends);
  const store = useAppStore();
  const { load, refresh, refreshing } = useAggregate(weeks, repo, fetchImpl);
  const data = load?.state === "ready" ? load.data : null;
  const status = (
    <span className="agg-fetched">
      {data
        ? `Fetched ${new Date(data.fetched_at).toLocaleTimeString()}, ${data.refreshed} PR${data.refreshed === 1 ? "" : "s"} refreshed from GitHub. `
        : ""}
      <button type="button" className="agg-refresh" disabled={refreshing} onClick={refresh}>
        {refreshing ? "Refreshing…" : "Refresh"}
      </button>
    </span>
  );
  return (
    <>
      <FilterBar counts={counts} aggregate={status} />
      {load === null ? (
        <div className="empty">{`Loading the last ${weeks} weeks…`}</div>
      ) : load.state === "error" ? (
        <div className="empty error">{`Could not load the aggregate: ${load.message}`}</div>
      ) : (
        <AggregatePanels
          data={load.data}
          excludeWeekends={weekends === "exclude"}
          onExcludeWeekends={(exclude) =>
            store.getState().setView({ ...store.getState().view, weekends: exclude ? "exclude" : "" })
          }
        />
      )}
    </>
  );
}

export function AggregatePanels({
  data,
  timeZone = browserTimeZone(),
  excludeWeekends = false,
  onExcludeWeekends,
}: {
  data: Aggregate;
  /** The viewer's IANA zone: what a day, a week and a weekend are. */
  timeZone?: string;
  excludeWeekends?: boolean;
  onExcludeWeekends?: (exclude: boolean) => void;
}) {
  const catalog = useApp((s) => s.catalog);
  const { mix, coverage } = data;
  const opened = catalog?.events.opened;
  const merged = catalog?.events.merged;
  const now = Date.parse(data.window_end);
  const weeks = data.weeks;
  const start = windowStart(now, weeks, timeZone);
  const inWindow = data.prs.filter((pr) => {
    const times = [pr.opened, pr.ended].filter((t): t is string => t !== null).map((t) => Date.parse(t));
    return times.some((t) => t >= start && t <= now);
  });
  const flow = throughput(data.prs, { windowStart: start, now, weeks, timeZone });
  const weekdayFlow = throughput(data.prs, { windowStart: start, now, weeks, timeZone, excludeWeekends: true });
  const linesFlow = linesThroughput(data.prs, { windowStart: start, now, weeks, timeZone });
  const weekdayLines = linesThroughput(data.prs, { windowStart: start, now, weeks, timeZone, excludeWeekends: true });
  const lead = leadTimeSeries(data.prs, {
    windowStart: start,
    now,
    days: bucketDays(weeks),
    timeZone,
    excludeWeekends,
  });
  const withStart = inWindow.filter((pr) => pr.task_started !== null);
  const byName = withStart.filter((pr) => pr.matched_by === "name").length;

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
    { key: "opened", label: "PRs opened", tone: opened?.tone ?? "green", legend: "PRs opened in each local day or week." },
    { key: "merged", label: "PRs merged", tone: merged?.tone ?? "purple", legend: "PRs merged in each local day or week." },
  ]);
  const linesStyles = withHatches([
    { key: "added", label: "Lines added", tone: "green", legend: "Lines added by the PRs merged in each local day or week." },
    { key: "deleted", label: "Lines deleted", tone: "red", legend: "Lines deleted by the PRs merged in each local day or week." },
  ]);
  const leadStyles = withHatches(
    STAGES.map(({ key, label }) => ({
      key,
      label,
      tone: LEAD_TONES[key],
      legend: `${LEAD_DESCRIPTIONS[key]} Each bar is the median over the PRs merged in a group.`,
    })),
  ) as LeadStageStyle[];
  const startsAfterWindow = Date.parse(coverage.task_starts_since) > start;

  return (
    <section className="aggregate" aria-label="Aggregate">
      <p className="agg-summary">
        {data.repo !== null && (
          <strong className="agg-repo-n">{`${data.repo}: n = ${inWindow.length} PR${inWindow.length === 1 ? "" : "s"}. `}</strong>
        )}
        {`${formatLocalDate(start, timeZone)} to ${formatLocalDate(now, timeZone)} (${data.weeks} weeks, ${timeZone}): ${inWindow.length} of your PRs`}
        {coverage.tasks_without_pr > 0 && `, ${coverage.tasks_without_pr} task${coverage.tasks_without_pr === 1 ? "" : "s"} without a PR yet`}
        {"."}
      </p>
      <div className="agg-notices">
        {inWindow.length === 0 && (
          <div className="notice">{`None of your PRs were updated in the last ${data.weeks} weeks.`}</div>
        )}
        {startsAfterWindow && inWindow.length > 0 && (
          <div className="notice">
            {`Task starts are only recorded since ${formatBucketDate(coverage.task_starts_since)}: ${withStart.length} of ${inWindow.length} PRs have one. Earlier PRs begin when they were opened, and their "task started" intervals are unknown rather than guessed.`}
          </div>
        )}
        {byName > 0 && (
          <div className="notice">
            {`${byName} of ${withStart.length} task starts matched by branch name (their PR was never recorded, so a reused branch name could be mismatched). The rest are tied to their PR by id.`}
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
        data={flow}
        weekdayData={weekdayFlow}
        timeZone={timeZone}
        styles={{ opened: throughputStyles[0], merged: throughputStyles[1] }}
      />
      <LinesChart
        data={linesFlow}
        weekdayData={weekdayLines}
        timeZone={timeZone}
        styles={{ added: linesStyles[0], deleted: linesStyles[1] }}
      />
      <LeadTimeChart
        series={lead}
        styles={leadStyles}
        timeZone={timeZone}
        windowStart={start}
        now={now}
        modeLabel={excludeWeekends ? "Excluding weekends" : "All time"}
        excludeWeekends={excludeWeekends}
        onModeChange={(exclude) => onExcludeWeekends?.(exclude)}
        startsSince={coverage.task_starts_since}
      />
      <MixChart
        title="Status mix"
        description="A stacked snapshot at every task state change, with each change at its exact time on the x-axis. Each task in flight has one state, so the shares sum to 100%. Gaps mean nothing was in flight. Merge conflicts and unresolved comments have no history, so they are not separated."
        series={stateSeries}
        shares={mix.series.map((s) => s.share)}
        counts={mix.series.map((s) => s.count)}
        starts={mix.starts}
        inflight={mix.inflight}
        timeZone={timeZone}
        empty={`No task was in flight in this window${startsAfterWindow ? ` (task starts are recorded since ${formatBucketDate(coverage.task_starts_since)})` : ""}.`}
        transitionTicks={weeks <= 12}
      />
      <MixChart
        title="Who is it waiting on"
        description="Stacked snapshots at every task state change, placed at their exact times on the x-axis and grouped by who the next action waits on: you (open, publish, fix, address review, merge), CI, or others (a reviewer, or a parent PR that waits on one). Derived with the rules of the Next column."
        series={whoSeries}
        shares={mix.who.map((s) => s.share)}
        counts={mix.who.map((s) => s.count)}
        starts={mix.starts}
        inflight={mix.inflight}
        timeZone={timeZone}
        empty="No task was in flight in this window."
        transitionTicks={weeks <= 12}
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
