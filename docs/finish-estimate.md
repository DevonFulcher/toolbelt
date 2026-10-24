# Finish estimate

Each task card says when its PR will be merged: "~2d (p50), up to 4d (p80)",
with the reasoning in its tooltip ("waiting on ann, 2.1 working days in, based
on 14 reviews"), or "unknown: not enough history". The aggregate view's
**Estimate accuracy** section says how right those estimates have been. There
is no LLM and nothing random: the same history gives the same estimate.

Code: `src/toolbelt/finish_estimate/` (pure: `working_hours.py`, `model.py`,
`backtest.py`, `config.py`) and `src/toolbelt/ui/estimate_feed.py` (reads and
writes the stack DB, the job, the card and accuracy payloads).

## What is stored

| Table (stack DB) | What |
| --- | --- |
| `task_stage_durations` | One row per stage of a task that ended: `task_key` (`owner/name#number`), `stage` (a kind of Next: `wait_review`, `address_review`, `wait_ci`, `fix_ci`, `request_review`, `merge`, `blocked`, ...), `kind` (who it waited on: `you`, `others`, `ci`, ...), `started_at`, `ended_at`, `actor`, `reviewer`, `repo`, `additions`, `deletions`, `size_bucket`, `round`, and `finished_at` when the task merged. |
| `predictions` | The estimate shown, once per stage a task enters (`p50_at`, `p80_at` as UTC instants, `level`, `n`, `model_version`), later joined with the task's actual finish. |
| `estimate_backtests` | Each backtest: when it ran, the hyperparameters it chose and its metrics (JSON). The live estimator uses the latest one's parameters. |

The stage rows are rebuilt from the cached GitHub history (the aggregate's
PRs, matched to the stack DB's tracked and removed branches) by the same fold
the card and the aggregate use, `aggregate.task_segments` and `with_blocked`:
a stage is a run of one Next kind. That is also the backfill: everything the
aggregate has cached becomes history, with no network. The reviewer is the
subject of the review request the stage waited on (`pr_activity.reviewer_of`;
`aggregate_feed.parse_pr` now also fills `requested_reviewers`).

## Working time

Durations are measured in **working time**: Monday to Friday, 09:00-17:00
local by default, so a request on Friday at 18:00 effectively starts Monday at
09:00. Configure it in the config file (quote the times: YAML reads an
unquoted `09:00` as a number):

```yaml
estimate:
  working_hours:
    days: [mon, tue, wed, thu, fri]
    start: "09:00"
    end: "17:00"
    timezone: America/Chicago   # default: this machine's zone
```

The server turns a remaining working time into a UTC instant
(`WorkingHours.advance`) and sends instants; the page formats them
(`web/src/logic/localTime.ts`). There is no holiday calendar.

## The estimate

For a task in stage *s* with *e* working seconds already spent in it
(`model.estimate`):

1. **Samples.** The stage's ended samples (the latest 300), each weighted by
   recency: `w = 0.5 ** (age_weeks / half_life_weeks)`.
2. **Groups and factors.** Each stage has dimensions that explain it
   (`STAGE_DIMS`: the reviewer and review round for `wait_review`; the reviewer
   and size for `address_review`; the size for work stages; the repo for CI
   stages; the repo by default). Size is `log2(additions + deletions)`. For a
   group *g* of *n* samples with weighted median `m_g`, and the stage's `m_s`:

   ```
   raw factor     r_g = m_g / m_s
   shrunk factor  f_g = exp( n / (n + k) * (log m_g - log m_s) )
   ```

   `k` is the shrinkage. A group with few samples has `f_g` near 1: it is pulled
   to the stage median. (This is `n/(n+k)`, not `k/(k+n)`: the latter would give
   a group of one sample the whole effect.) A group with fewer than
   `min_samples` has no factor.
3. **Adjust.** Each sample `d_i` becomes `d_i * F_task / R_i`: divided by its
   own groups' raw factors (what is left is how a group's samples vary
   *within* it) and multiplied by the task's shrunk factors. The change is
   limited to a factor of 10 either way, so a stage that is often instant
   cannot blow up the others.
4. **Survival.** Only samples with `d' > e` remain: a review that has already
   lasted two days is compared with reviews that lasted longer. The remaining
   time is the weighted quantile of `d' - e` (`q`: the smallest value whose
   cumulative weight reaches `q` of the total, no interpolation).
5. **The rest of the path.** Each sample also has the working time its task
   needed after that stage ended until it merged (known only once the task
   merged). The total is the quantile of `d' - e + tail` over the same samples,
   so what makes tasks slow together (a slow reviewer, then a slow merge) stays
   together. With fewer than 3 such samples the later stages are added as their
   own quantiles, and with none the estimate says "not counting later stages".
6. **Overdue.** When no sample lasted as long as the stage already has, the
   answer is a floor (a quarter of the stage's typical duration, at least an
   hour, plus the later stages) and says so.
7. **Stacked tasks.** A task whose base is another open PR finishes no sooner
   than the parent: `finish = max(own finish, parent finish + post)`, where
   `post` is what usually followed a "blocked on parent" stage (else how long
   the merge stage takes). p50 and p80 are combined separately, which is
   conservative.

**Fallback levels** (`level` in the payload): the dimensions whose group for
this task had enough samples, as `reviewer+size`, `reviewer`, `size`, ...; then
`stage` (no group, the whole stage); then `global` (the stage has fewer than
`min_samples`, so the latest samples of every stage are pooled, with no groups
and no later stages); then `unknown`. An unknown estimate has no number: it is
never invented.

## Backtesting

`finish_estimate.backtest`: for each of the 150 most recently finished tasks,
at the start of each of its stages, it predicts the finish from **only** the
history strictly before that instant (a sample counts once its stage ended
before it, and its remainder only once its task merged before it), and compares
with the actual working time left. Per stage and overall it reports:

- **coverage**: the share of predictions whose actual time was at or under the
  p80 (the target is 80%);
- **median error**: the median of `|actual - p50|`;
- **width**: the median of `p80 - p50`;
- how many predictions were unknown.

**Tuning.** The same replay is run for a small fixed grid of
`half_life_weeks` (12, 4, 52), `min_samples` (5, 3, 10) and `shrinkage` (5, 2,
10), and the point with the lowest mean pinball loss (at the 0.5 and 0.8
quantiles) is stored with the metrics. An unknown prediction is scored as "no
time left", so a setting cannot win by declining to answer. A tie keeps the
earlier point (defaults first). The live estimates use the latest stored
parameters.

**Regime change.** For each stage with at least 8 samples in each of the last 4
weeks and the 4 before, a change of the median beyond 30% is reported ("wait
review estimates are now 45% shorter than 4 weeks ago").

**When it runs.** `tt ui` rebuilds the stage rows every 15 minutes (and shortly
after start, once the GitHub history is loaded) and runs a backtest when the
last one is a day old. To run one now, without the network:

```sh
toolbelt estimate backtest
```

## Reading the accuracy section

- **Coverage near 80%** is the goal. Well below, the p80 is too optimistic
  (estimates are too short); well above, it is too cautious (too wide to be
  useful: look at the range too). With few predictions per stage a percentage
  is coarse: the count is next to it.
- **Median error** is how far the p50 usually is, in working time.
- A **regime change** notice means the recent weeks differ from the weeks
  before: the half-life should shorten at the next backtest.
- **Estimates shown so far** compares what a card actually said (`predictions`)
  with the finish of tasks that have merged since.

## When it is off

- *Many "unknown"*: not enough merged PRs in the cached history for that stage.
  Widen the aggregate window once (26 or 52 weeks) so more GitHub history is
  cached, then run `toolbelt estimate backtest`.
- *Coverage far from 80% for one stage*: look at that stage's rows
  (`select * from task_stage_durations where stage = '...'`): a stage that is
  mostly instant, or whose Next rule changed, is the usual cause.
- *Times look wrong around the day*: check `estimate.working_hours` (the zone
  and hours).
- *Estimates ignore a change of pace*: the half-life is tuned from the grid;
  run the backtest after a change of pace to pick a shorter one.

## Known limits

- The history is the user's own PRs; a reviewer is a key with the samples of
  that person only.
- Predictions are backtested at stage boundaries, not mid-stage, so the
  survival conditioning is tested only by the live predictions.
- Working hours are one calendar, with no holidays and no time off.
- Raw factors of several dimensions of a stage are multiplied as if they were
  independent (a reviewer and a round that go together are partly counted twice).
