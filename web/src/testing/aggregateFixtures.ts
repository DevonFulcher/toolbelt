// A small `Aggregate` payload for the aggregate view's tests.
import type { Aggregate, LanePhaseKind, PrMilestones, WaitingOnKey } from "../api/generated";

/** The Next kinds the fixture's status chart uses, in stacking order. */
const KINDS: LanePhaseKind[] = [
  "next_open_pr",
  "next_publish",
  "next_request_review",
  "next_wait_review",
  "next_address_review",
  "next_wait_ci",
  "next_fix_ci",
  "next_merge",
  "next_blocked",
  "next_claimed",
];

function milestones(number: number, parts: Partial<PrMilestones>): PrMilestones {
  return {
    repo: "o/api",
    number,
    title: `PR ${number}`,
    url: `https://github.com/o/api/pull/${number}`,
    state: "OPEN",
    opened: "2026-10-06T09:00:00Z",
    ended: null,
    task_started: null,
    matched_by: null,
    first_review_requested: null,
    first_review: null,
    first_review_after_request: null,
    additions: 10,
    deletions: 2,
    ...parts,
  };
}

/** Merged: 1 and 2 (known starts, requests and reviews), 3 (request and review,
 * no start) and 4 (nothing known). Closed: 5. Open: 6 (start matched by name),
 * 7, 8, 9. */
const PRS: PrMilestones[] = [
  milestones(1, {
    state: "MERGED",
    opened: "2026-09-24T09:00:00Z",
    ended: "2026-09-25T15:00:00Z",
    task_started: "2026-09-24T04:00:00Z",
    matched_by: "id",
    first_review_requested: "2026-09-24T10:00:00Z",
    first_review: "2026-09-24T18:00:00Z",
    first_review_after_request: "2026-09-24T18:00:00Z",
  }),
  milestones(2, {
    state: "MERGED",
    opened: "2026-09-29T09:00:00Z",
    ended: "2026-10-01T09:00:00Z",
    task_started: "2026-09-29T03:00:00Z",
    matched_by: "id",
    first_review_requested: "2026-09-29T11:00:00Z",
    first_review: "2026-09-30T11:00:00Z",
    first_review_after_request: "2026-09-30T11:00:00Z",
  }),
  milestones(3, {
    state: "MERGED",
    opened: "2026-10-02T09:00:00Z",
    ended: "2026-10-05T10:00:00Z",
    first_review_requested: "2026-10-02T10:00:00Z",
    first_review: "2026-10-03T09:00:00Z",
    first_review_after_request: "2026-10-03T09:00:00Z",
  }),
  milestones(4, { state: "MERGED", opened: "2026-10-05T08:00:00Z", ended: "2026-10-06T10:00:00Z" }),
  milestones(5, { state: "CLOSED", opened: "2026-09-30T09:00:00Z", ended: "2026-10-01T09:00:00Z" }),
  milestones(6, { task_started: "2026-10-05T20:00:00Z", matched_by: "name" }),
  milestones(7, { opened: "2026-10-06T10:00:00Z" }),
  milestones(8, { opened: "2026-10-07T10:00:00Z" }),
  milestones(9, { opened: "2026-09-22T10:00:00Z" }),
];

/** Nine PRs, and four sampled instants of status mix. The third has nothing
 * in flight. Dates are UTC (the tests run in it); window end is a Thursday. */
export function aggregate(overrides: Partial<Aggregate> = {}): Aggregate {
  const days = ["2026-10-05T00:00:00Z", "2026-10-05T06:00:00Z", "2026-10-05T12:00:00Z", "2026-10-05T18:00:00Z"];
  const inflight = [2, 4, 0, 10];
  // Shares per sample for the ten Next kinds; null where nothing was in flight.
  const columns: (number[] | null)[] = [
    [0.5, 0.5, 0, 0, 0, 0, 0, 0, 0, 0],
    [0.25, 0.25, 0.25, 0.25, 0, 0, 0, 0, 0, 0],
    null,
    [0, 0, 0, 0.4, 0.1, 0, 0.1, 0.4, 0, 0],
  ];
  const whoOf = (c: number[]): number[] => [
    c[0] + c[1] + c[2] + c[4] + c[6] + c[7],
    c[9],
    0,
    c[5],
    c[3] + c[8],
  ];
  const who: WaitingOnKey[] = ["you", "agent", "auto", "ci", "others"];
  return {
    weeks: 12,
    window_start: "2026-09-21T00:00:00Z",
    window_end: "2026-10-08T12:00:00Z",
    fetched_at: "2026-10-08T11:58:00Z",
    refreshed: 4,
    repo: null,
    prs: PRS,
    samples: { starts: days, inflight },
    waiting_on: who.map((key, k) => ({
      key,
      share: columns.map((c) => (c ? whoOf(c)[k] : null)),
      count: columns.map((c, i) => (c ? Math.round(whoOf(c)[k] * inflight[i]) : 0)),
    })),
    lanes: [
      {
        lane: "next",
        events: [],
        phases: KINDS.map((kind, k) => ({
          kind,
          share: columns.map((c) => (c ? c[k] : null)),
          count: columns.map((c, i) => (c ? Math.round(c[k] * inflight[i]) : 0)),
        })),
      },
    ],
    coverage: {
      task_starts_since: "2026-09-30T08:00:00Z",
      tasks_without_pr: 1,
      truncated: false,
      hidden: 0,
    },
    notes: [{ label: "Waiting on CI", why: "From a push until its checks completed." }],
    ...overrides,
  };
}
