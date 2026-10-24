# Finish estimate

Each task card says when its PR will be merged: "~2d (p50), up to 4d (p80)",
with the reasoning in its tooltip ("waiting on ann, 2.1 working days in, based
on 14 reviews"), or "unknown: not enough history". The aggregate view's
**Estimate accuracy** section says how right those estimates have been. There
is no LLM and nothing random: the same history gives the same estimate.

Code: `src/toolbelt/finish_estimate/` (pure: `working_hours.py`, `model.py`,
`features.py`, `snapshots.py`, `forecast.py`, `baseline.py`, `challengers.py`,
`backtest.py`, `config.py`) and `src/toolbelt/ui/estimate_feed.py` (reads and
writes the stack DB, the job, the card and accuracy payloads).

## What is stored

| Table (stack DB) | What |
| --- | --- |
| `task_stage_durations` | One row per stage of a task that ended: `task_key` (`owner/name#number`), `stage` (a kind of Next: `wait_review`, `address_review`, `wait_ci`, `fix_ci`, `request_review`, `merge`, `blocked`, ...), `kind` (who it waited on: `you`, `others`, `ci`, ...), `started_at`, `ended_at`, `actor`, `reviewer`, `repo`, `additions`, `deletions`, `size_bucket`, `round`, and `finished_at` when the task merged. |
| `predictions` | The estimate shown (the champion's), once per stage a task enters (`p50_at`, `p80_at` as UTC instants, `level`, `n`, `model`, `model_version`), later joined with the task's actual finish. |
| `estimate_backtests` | Each backtest: when it ran and its metrics (JSON): every model's metrics, the champion per stage with its evidence, the ablation, the regime changes. |
| `model_fits` | Each learned model's fitted state, when it was fitted and on how many rows, so a restart does not refit. |

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

## Several models, one interface

A **model** (`finish_estimate/forecast.py`) is anything with `name`, `version`,
`fit(TrainingData)` and `predict(SnapshotFeatures) -> Forecast`, plus
`dump()`/`load()` so a fit survives a restart. `TrainingData` is only what was
known strictly before `known_at`: ended stages (a task's finish counts only if
it came before) and snapshot rows whose targets are right-censored at
`known_at`. A `Forecast` is the working time left to the task's finish at p50
and p80 (never a made-up number: `None` when it has no answer), optionally the
whole distribution on a fixed 19-quantile grid, and a JSON-able `details`
payload that says what it learned.

To add a model: write the class, add it to `CHALLENGERS`
(`challengers.py`). The backtest harness runs it on the same rows and splits as
the others, the champion logic considers it, the popup and the accuracy
section list it. Nothing else is per model.

| Model | What it is | What it can express |
| --- | --- | --- |
| `baseline` | The recency-weighted empirical stage distribution conditioned on the time already spent (below). No fit: it reads the stored history. | Reviewer, size, repo and round as shrunk factors; the stages after as the same tasks' remainders. |
| `regression` | Linear quantile regression of the log of the remaining time at p50 and p80, one per stage, on the standardized registry features; the reviewer through their trailing latency (not a one-hot). | Smooth numeric effects (size, queue length, hour of the week). |
| `hazard` | Discrete-time hazard: logistic regression of "finishes in this working-hour bin given it has not yet", one per stage; a row checkpointed after `e` hours enters at the bin containing `e` (delayed entry), so the time already waited is native, and an open task contributes the bins it was observed through. | A fast group and a slow group (a non-monotone hazard), censoring. |
| `trees` | `HistGradientBoostingRegressor(loss="quantile")` for p50 and p80 of the log remaining time; the stage, reviewer and repo are categorical. Early stopping, a minimum leaf size and a cap of 50,000 rows guard against overfitting. | Interactions (a reviewer by weekday). |

The learned models fit on **snapshot rows** (below), in a background thread
(`tt ui`'s job; `fit` and `predict` import scikit-learn lazily, so `tt`
start-up is unaffected), deterministically (`random_state=0`). Quantile
regression and trees treat a censored row in two passes: first as observed at
its bound; a censored row whose bound is below the first fit's prediction could
end anywhere above it, so it is dropped for the second fit. p80 is never below
p50 (a crossing is clamped), and a stage with fewer than 10 training rows
answers unknown.

## The feature registry

`finish_estimate/features.py` declares every input once: a feature has a name,
an encoding (`categorical`, `numeric`, `log`), the stages it applies to, and how
it is computed from facts known strictly before an instant. The snapshot
dataset, the baseline's factor dimensions and the learned models' inputs read
it. Adding a feature is one `Feature` there (a column of `task_stage_durations`
only when it must be stored: its `sample_attr`). `STAGE_DIMS` lists the baseline
dimensions for every kind a stage can be; a test fails for a Next kind without
an entry (`DEFAULT_DIMS` is allowed, silently defaulting is not).

| Feature | Meaning |
| --- | --- |
| `reviewer`, `round`, `size`, `repo` | Stored columns: who the stage waits on, how many times the task was in the stage, log2 of lines changed, the repo (categorical). |
| `size_log2` | The size bucket as a number. |
| `hour_of_week` | Hours since Monday 00:00 in the working-hours zone. |
| `reviewer_latency` | The reviewer's median latency over their last 20 reviews that ended before the instant (log). |
| `reviewer_open_requests` | Review requests of theirs on other tasks open at the instant. |
| `repo_ci_seconds` | The repo's typical CI wait before the instant (log). |
| `queue_length` | Other tasks waiting on the user at the instant (his queue). |
| `slack_hours_since` | Hours since the task's last Slack post or bump (log). From the recorded Slack posts; no new data is fetched. |

## The snapshot dataset

`snapshots.py`: one row per (task, stage, checkpoint instant); checkpoints are
the start of a stage and each full working day in it. Features use only what
ended strictly before the checkpoint (a review ending at the checkpoint is not
counted). Targets are the working time left to the end of the stage and to the
task's finish. A task still open at the end of the data is **right-censored**:
its targets are the time observed so far, a lower bound, flagged
`*_censored`. It is kept (dropping it would bias estimates short); the
harness reports how many predictions were for open tasks and a coverage that
counts those already past their p80 as the misses they are. A task closed
without merging has a stage target and no finish target.

## The baseline

For a task in stage *s* with *e* working seconds already spent in it
(`model.estimate`):

1. **Samples.** The stage's ended samples (the latest 300), each weighted by
   recency: `w = 0.5 ** (age_weeks / half_life_weeks)`.
2. **Groups and factors.** The stage's dimensions (`STAGE_DIMS`: the reviewer
   and review round for `wait_review`; the reviewer and size for
   `address_review`; the size for work stages; the repo for CI stages). For a
   group *g* of *n* samples with weighted median `m_g`, and the stage's `m_s`:

   ```
   raw factor     r_g = m_g / m_s
   shrunk factor  f_g = exp( n / (n + k) * (log m_g - log m_s) )
   ```

   `k` is the shrinkage: a group with few samples has `f_g` near 1, pulled to the
   stage median. A group with fewer than `min_samples` has no factor.
3. **Adjust.** Each sample becomes `d_i * F_task / R_i` (its own groups' raw
   factors out, the task's shrunk factors in), limited to a factor of 10 either
   way.
4. **Survival.** Only samples with `d' > e` remain; the remaining time is the
   weighted quantile of `d' - e` (the smallest value whose cumulative weight
   reaches `q`, no interpolation).
5. **The rest of the path.** Each sample also has the working time its task
   needed after that stage until it merged, so the total is the quantile of
   `d' - e + tail` over the same samples (what makes tasks slow together stays
   together). With fewer than 3 such samples the later stages are added as their
   own quantiles, with none the estimate says "not counting later stages".
6. **Overdue.** When no sample lasted as long as the stage already has, the
   answer is a floor (a quarter of the stage's typical duration, at least an
   hour, plus the later stages) and says so.

A stage with fewer than `min_samples` samples is **unknown** ("no history for
this stage yet"); it is never answered from other stages. Configure a prior for
it if you want a number (`estimate.priors`, working hours at p50 and p80).

## Stacked tasks

Ancestors are never summed: a child's own work (writing, review, CI) runs in
parallel with its parent's; only the merge is ordered. Per model, the finish of
a task whose base is another open PR is

```
finish = max(own finish, parent finish) + post-parent tail
```

with only the **nearest** parent (its finish already includes the grandparent by
the same rule: recurse up the chain). The maximum is combined at the
distribution level, as the product of the two CDFs on the 19-quantile grid
(assuming independence; a slow week slows both, so the true maximum is earlier
than the product says and the estimate errs late), and the tail (what followed a
"blocked on parent" stage, else how long the merge stage takes) is added by
convolving the grids. A task in the "blocked" stage has already done its own
work: its own finish is now. A parent that is merged is not a parent (no wait);
a parent with no estimate makes the child "at least its own", never a made-up
number; siblings never affect each other. A parent that was rewritten or
squash-merged adds a restack and a CI re-run, which is what the tail's history
learns from. The popup shows the parent wait as its own row.

## Backtesting and champions

`finish_estimate/backtest.py`: every model runs through one harness
(`replay`). At each checkpoint of the 100 most recently active tasks it is
(re)fitted on what was known strictly before (at most every two days: a stale
fit is still leak-safe) and predicts the finish; the actual working time left
is compared. Per model, per stage and overall, it reports:

- **pinball loss** at p50 and p80, in seconds (an unknown prediction is scored
  as "no time left": a model cannot win by declining to answer);
- **coverage**: the share of predictions whose actual time was at or under the
  p80 (the target is 80%), and the same counting open tasks already past their
  p80 as misses;
- **median error** of the p50 and **width** (median p80 - p50);
- how many predictions were unknown and how many were for open tasks.

**Champions.** Per stage (and `all`), the best model by pinball loss is promoted
only when it beats the baseline's by at least 5% on at least 30 scored test
points; otherwise the baseline stays. The champion, the margin, the points and
every model's evidence are stored with the backtest, so a change of champion is
visible over time. The card shows the champion's range; the popup shows all.

**Ablation.** The same replay takes a feature out of the regression model, one
at a time, on the same rows: the pinball-loss change and the number of test
points say whether the feature carries signal (positive) or is noise (about
zero or negative).

**Regime change.** For each stage with at least 8 samples in each of the last 4
weeks and the 4 before, a change of the median beyond 30% is reported ("wait
review estimates are now 45% shorter than 4 weeks ago").

**When it runs.** `tt ui` rebuilds the stage rows every 15 minutes (and shortly
after start, once the GitHub history is loaded), refits a learned model when
its fit is a day old or 25 stages have been stored since (the fit is stored in
`model_fits`: a restart restores it), and runs the backtest when the last one is
a day old. `toolbelt estimate backtest` does all of it now, without the network.
The request path only reads what the background computation stored (the cards'
extras and the popup's `GET /api/estimate?id=...`).

## Reading the accuracy section

- **Coverage near 80%** is the goal. Well below, the p80 is too optimistic
  (estimates are too short); well above, it is too cautious (too wide to be
  useful: look at the range too). With few predictions per stage a percentage
  is coarse: the count is next to it.
- **Median error** is how far the p50 usually is, in working time.
- A **regime change** notice means the recent weeks differ from the weeks
  before: the half-life should shorten at the next backtest.
- **Models** compares every model on the same test points: lower pinball loss is
  better; coverage should sit near 80%; "open tasks" are predictions for tasks
  still going. The **champion** table says who answers for each stage and by how
  much it beat the baseline; a champion changing is listed below it.
- **Which features carry signal** lists the loss change when a feature is
  removed (with its test points): a large positive change is signal, around zero
  or negative is noise.
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
- *Estimates ignore a change of pace*: the baseline's half-life is fixed
  (`Params`); the learned models refit daily. A regime-change notice says which
  stage moved.

## Known limits

- The history is the user's own PRs; a reviewer is a key with the samples of
  that person only.
- Backtest checkpoints are stage starts and each full working day in a stage,
  not every instant.
- Working hours are one calendar, with no holidays and no time off.
- Raw factors of several dimensions of a stage are multiplied as if they were
  independent (a reviewer and a round that go together are partly counted twice).
