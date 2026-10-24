# Timeline lanes

A card's timeline is a PR track with lanes under it, all on one time axis.

- The **PR track** is the PR's own history from GitHub (`pr_states.py`:
  `EVENTS`, `PHASES`, the "now" chips).
- A **lane** is any other source of status: `git` (conflicts), `ci` (rollup
  changes `tt ui` saw), `agent` (autonomous runs), `claim`, `slack`,
  `merge_queue`, `decision`, `task` (tracker moves). Each is a `LaneSource` in
  `src/toolbelt/ui/timeline_lanes.py`: its label, the markers (`events`) and
  shaded spans (`phases`) it can draw, and `covers`, the status types it draws.

## Adding a status

1. Add the status type (a `Literal` alias or enum) to `api_models.py` as usual.
2. Add its values to a lane's `covers` (a `Coverage` mapping each value to the
   kinds that show it, or `()` for "shown by not drawing"), adding the marker or
   span kinds to that lane, or a whole new `LaneSource`.
3. Read its history in `lane_history.py` (`build_lanes`), from a table that
   records when it happened.
4. `pnpm --dir web gen:types`.

`tests/test_timeline_lanes.py` fails until step 2 is done: every status type in
`api_models` must be covered by a lane, drawn by the PR track, or listed in
`NOT_TIMELINE_STATUS` with the reason it has no history. The wire's
`LaneEventKind` / `LanePhaseKind` and the client's `catalog.lane_events` /
`catalog.lane_phases` are built from the registry, so the renderer
(`web/src/logic/lanes.ts`, `Timeline.tsx`) and the legend need no per-kind code.

## Where history comes from

Every lane item belongs to a task by its `task_id` (a branch name is reused, a
task id is not): the store resolves it from the tracked branch when a row is
written (`status_transitions`, `merge_queue`, `task_status_moves`), and
`agent_runs`, `decisions` and `claim_spans` carry it. Slack posts belong to the
task through its PR. A span still open when its task was archived ends at the
archiving. Rows that predate the `task_id` columns were assigned to the task
alive at their time.

| Lane | Recorded in |
| --- | --- |
| git, ci | `status_transitions` (written by the status refresher when a lookup differs from the last one recorded; the first lookup records a baseline, so history starts when `tt ui` first saw the branch) |
| agent | `agent_runs` |
| claim | `claim_spans` (the claim kind and holder kind pick the span) |
| slack | `slack_posts` (matched to the branch by repo and PR number) |
| merge_queue | `merge_queue` (only while queued) |
| decision | `decisions` |
| task | `task_status_moves` |

Lanes reach the page in `BranchExtras.lanes`, rebuilt whenever the database
changes (`PushedViews`).

## Next is a lane, and the aggregate view aggregates lanes

A task's status over time is its **Next** (`next_action.py`), the same one the
card shows. `aggregate.task_segments` runs `next_action` itself at every moment
something it reads changed, on the PR's replayed state plus the recorded lanes
(conflicts, open review threads, working claims, pending decisions, Slack
posts). The result is the "next" lane: first on the card timeline, and the
status chart of the aggregate view. There is no separate "status mix" to keep in
step: a new Next rule is a new kind of the "next" lane (the registry test makes
sure), and the aggregate and the card both fold the same function.

The aggregate view is an aggregation of the lanes:

- `Aggregate.lanes` has one layer per lane without an `aggregate_skip` reason
  (the CI lane is skipped: the replay already has CI from GitHub's history).
  Each layer is, per sampled instant, how many tasks in flight had a span of
  each kind of the lane, plus the times of its markers.
- `Aggregate.waiting_on` groups the "next" lane by who its next action waits on.
- `Aggregate.samples` are the instants everything is sampled at (every change
  of a task or lane) and how many tasks were in flight.
- "Blocked on a parent" needs the other tasks (`aggregate.with_blocked`): the
  card's lane and the aggregate's use the PR's base-branch changes
  (`PrFacts.base_changes`) the same way.
- Lane history is recorded history: it covers tasks that were tracked branches
  and begins when each source was first recorded; before that Next is what the
  PR's own timeline gives. The Slack rules apply from the first recorded post.
  It is joined to a task by `task_id`.
