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

## The aggregate view

The aggregate view uses the same registry (`aggregate.py`):

- Time under a claim is the status mix's "With an agent" state, replacing the
  PR's own state while the claim was held.
- Each lane without an `aggregate_skip` reason gets a layer in
  `Aggregate.lanes`: per sampled instant, how many of the tasks in flight had a
  span of each kind (drawn like the status mix, the rest as "no activity"), and
  the times of its markers. A lane that is not counted says why in
  `LaneSource.aggregate_skip`; `tests/test_ui_aggregate.py` checks that every
  other lane has a layer.
- Lane history is recorded history: it covers tasks that were tracked
  branches and begins when each source was first recorded.
