// A small `Aggregate` payload for the aggregate view's tests.
import type { Aggregate, LeadStage, MixState, MixWho } from "../api/generated";

const HOUR = 3600;

const STATES: { key: MixState; label: string; tone: Aggregate["mix"]["states"][number]["tone"] }[] = [
  { key: "working", label: "Working (no PR yet)", tone: "accent" },
  { key: "draft", label: "In draft", tone: "gray" },
  { key: "ready", label: "Request review", tone: "accent" },
  { key: "waiting", label: "Waiting for review", tone: "amber" },
  { key: "changes", label: "changes requested", tone: "red" },
  { key: "ci_running", label: "Waiting on CI", tone: "amber" },
  { key: "ci_failing", label: "CI failing", tone: "red" },
  { key: "approved", label: "Approved", tone: "green" },
  { key: "blocked", label: "Blocked on parent", tone: "gray" },
];

function stage(key: LeadStage["key"], label: string, values: number[], unknown = 0): LeadStage {
  const sorted = [...values].sort((a, b) => a - b);
  const at = (f: number): number | null => (sorted.length ? sorted[Math.round((sorted.length - 1) * f)] : null);
  return {
    key,
    label,
    n: sorted.length,
    unknown,
    median_s: at(0.5),
    p75_s: at(0.75),
    p90_s: at(0.9),
    values_s: sorted,
  };
}

/** Four sampled instants of status mix over three weeks of throughput. The
 * third has nothing in flight. */
export function aggregate(overrides: Partial<Aggregate> = {}): Aggregate {
  const days = ["2026-10-05T00:00:00Z", "2026-10-05T06:00:00Z", "2026-10-05T12:00:00Z", "2026-10-05T18:00:00Z"];
  const inflight = [2, 4, 0, 10];
  // Shares per sample for the nine states; null where nothing was in flight.
  const columns: (number[] | null)[] = [
    [0.5, 0.5, 0, 0, 0, 0, 0, 0, 0],
    [0.25, 0.25, 0.25, 0.25, 0, 0, 0, 0, 0],
    null,
    [0, 0, 0, 0.4, 0.1, 0, 0.1, 0.4, 0],
  ];
  const whoOf = (c: number[]): [number, number, number] => [
    c[0] + c[1] + c[2] + c[4] + c[6] + c[7],
    c[5],
    c[3] + c[8],
  ];
  const who: MixWho[] = ["you", "ci", "others"];
  return {
    weeks: 12,
    window_start: "2026-09-21T00:00:00Z",
    window_end: "2026-10-08T12:00:00Z",
    fetched_at: "2026-10-08T11:58:00Z",
    refreshed: 4,
    throughput: {
      starts: ["2026-09-21T00:00:00Z", "2026-09-28T00:00:00Z", "2026-10-05T00:00:00Z"],
      opened: [2, 4, 3],
      merged: [1, 3, 2],
      closed: [0, 1, 0],
    },
    lead_time: [
      stage("start_to_open", "Task started to PR opened", [2 * HOUR, 5 * HOUR, 26 * HOUR], 3),
      stage("open_to_request", "PR opened to review requested", [0.5 * HOUR, 2 * HOUR, 3 * HOUR, 30 * HOUR]),
      stage("request_to_review", "Review requested to first review", [4 * HOUR, 9 * HOUR, 50 * HOUR]),
      stage("review_to_merge", "First review to merged", [6 * HOUR, 20 * HOUR, 24 * HOUR, 70 * HOUR]),
      stage("start_to_merge", "Task started to merged", []),
    ],
    mix: {
      step_hours: 6,
      starts: days,
      inflight,
      states: STATES.map((s) => ({ ...s, legend: `${s.label}.` })),
      series: STATES.map((s, k) => ({
        key: s.key,
        share: columns.map((c) => (c ? c[k] : null)),
        count: columns.map((c, i) => (c ? Math.round(c[k] * inflight[i]) : 0)),
      })),
      who: who.map((key, k) => ({
        key,
        share: columns.map((c) => (c ? whoOf(c)[k] : null)),
        count: columns.map((c, i) => (c ? Math.round(whoOf(c)[k] * inflight[i]) : 0)),
      })),
    },
    coverage: {
      prs: 9,
      prs_with_start: 3,
      starts_by_name: 1,
      task_starts_since: "2026-09-30T08:00:00Z",
      tasks_without_pr: 1,
      truncated: false,
      hidden: 0,
    },
    notes: [{ label: "Waiting on CI", why: "From a push until its checks completed." }],
    ...overrides,
  };
}
