# Finish estimate: features we do not use yet

The estimate (`finish-estimate.md`) uses a few things about a task today. This
page lists what it could use later, why each might matter, what data exists for
it, and how it could mislead. Every entry is a hypothesis: a feature stays out
until a backtest shows it helps (see "How to decide" below).

## What is used today

For the stage a task is in (a Next kind, so `wait_review`, `address_review`,
`fix_ci`, ...):

- the **stage** and the **working time already spent in it** (survival
  conditioning);
- the **reviewer** and the **review round** (for `wait_review`), the reviewer
  and **size** (for `address_review`), **size** for the work stages, the **repo**
  for CI stages and by default (`STAGE_DIMS` in `finish_estimate/model.py`).
  Size is `log2(additions + deletions)`;
- the **working-hours calendar** (one calendar, Mon to Fri 9 to 17 local by
  default), which turns durations into working time and back;
- **recency** (a sample's weight halves every `half_life_weeks`);
- for a stacked task, the **parent's finish** and what usually followed a
  "blocked on parent" stage.

Being evaluated by the work that adds the learned models and the ablation
harness (so not "unused", but not yet proven): the reviewer's trailing latency
and open-request load, the number of tasks waiting on the user, hours since the
last Slack post or bump, hour-of-week, and the repo's typical CI duration.

## Candidates

"Data today" says whether the input is already recorded (**have**), is
recorded only as a current value or a coarser one (**partial**), would need new
recording (**record**), would need a new GitHub or Jira fetch (**fetch**), or
has no deterministic source (**none**).

### People and queues

| Feature | Why it might matter | Stages | Expected signal | Data today | How it could mislead |
| --- | --- | --- | --- | --- | --- |
| Required approvals and approvals so far | A repo that needs two approvals takes longer than one that needs one, and the second approval is a separate wait. | `wait_review`, `merge` | Strong where it varies between repos | fetch (branch protection `requiredApprovingReviewCount`; approvals are in the timeline) | Constant within a repo, so it only helps across repos; the repo factor already absorbs it there. |
| Team review requests (a team, not a person) | Nobody in particular is responsible, so the wait is the fastest responder's, not one person's. | `wait_review` | Moderate | partial (the timeline has the team name; the reviewer key is one name) | The reviewer factor would pool unrelated teams if treated as a person. |
| Several reviewers requested | The wait ends at the first or last of them depending on who must approve. | `wait_review` | Moderate | have (timeline) | Which of them gates the merge is not in the timeline. |
| Reviewer's own working hours or time zone | A reviewer six time zones away is not waiting in your working hours. | `wait_review` | Moderate to strong for distributed teams | none (configure per reviewer if wanted) | Guessing from commit times is noise. |
| Reviewer out of office | A requested review will not move. | `wait_review` | Strong when true | none | No deterministic source; do not infer it. |
| Code area or path prefix touched | Different areas have different owners and reviewers, which matters before a reviewer is requested. | `request_review`, `wait_review` | Weak once the reviewer is known | fetch (file paths) | Redundant with the reviewer; overfits small areas. |

### The change itself

| Feature | Why it might matter | Stages | Expected signal | Data today | How it could mislead |
| --- | --- | --- | --- | --- | --- |
| Meaningful lines (excluding lockfiles, generated files, snapshots) | Raw size is inflated by lockfiles and generated code. | work stages, `address_review`, `wait_review` | Moderate, better than raw size | fetch (per-file additions) | Needs a definition of "generated" that holds across repos. |
| Files touched | A change across many files is harder to review than the same lines in one. | `wait_review`, `address_review` | Weak to moderate | fetch | Correlated with size; adds little once size is in. |
| Docs-only or tests-only | Trivial changes get fast approvals. | `wait_review` | Moderate | fetch (paths) | A path rule per repo; misclassifies mixed changes. |
| Task type (feature, fix, chore) | Fixes may move faster. | work stages | Weak | fetch (Jira issue type) | Weakly related; labels are inconsistent. |
| Number of open review threads | More threads mean more rework. | `address_review`, `resolve_comments` | Moderate for those two, none for `wait_review` | partial (only open/none is recorded as history; the count is current only) | Known only after the review arrives, so valid only for stages that start after it. |
| Commits and force-pushes since the review request | The author may still be changing the code. | `wait_review`, `address_review` | Weak | have (timeline) | Both directions plausible; noisy. |
| Description or test-plan present | Reviewers may start faster with context. | `wait_review` | Weak | record (the PR body is not stored) | A text rule; mostly noise. |

### Process and repo

| Feature | Why it might matter | Stages | Expected signal | Data today | How it could mislead |
| --- | --- | --- | --- | --- | --- |
| CI re-run or flake rate | A flaky repo adds rounds to CI stages. | `wait_ci`, `fix_ci`, `merge` | Moderate | fetch (GitHub Actions `run_attempt` only; re-runs are not timeline events) | Only Actions; other CI systems give no re-run data. |
| Merge queue state | A queued PR has a short, near-deterministic tail. | `merge` | Strong for that stage | partial (a row exists only while queued) | It is the stage itself, so it mostly restates it. |
| Sibling conflicts present | A conflict means a rebase before merging. | `merge`, the tail after a parent | Moderate | partial (pair results are cached by commit pair; the history of when a conflict existed is not recorded) | The conflict may be inherited or resolved by the time it matters. |
| Number of dependents (children waiting on this task) | Tasks others depend on get attention sooner. | stages waiting on the user | Small | have (stack tree) | Confounded with the user's priorities. |
| Agent run duration and trigger | A validate run's duration is near-deterministic by size. | the validate stage | Strong for that stage | have (`agent_runs`) | Only the stage that runs agents. |
| Agent activity of the task's turns (tool time, model time, blocked time) | How much the agent has already worked. | work stages of agent-driven tasks | Unknown | have (`agent_turns`, per task by worktree) | Interactive work is attributed only when a worktree resolves. |
| Effort level and prompt id of the turns | Higher effort may mean longer work; a prompt groups turns. | work stages | Unknown | record (events carry `effort.level` and `prompt_id`; not stored) | Not a stable proxy for difficulty. |

### Calendar and structure

| Feature | Why it might matter | Stages | Expected signal | Data today | How it could mislead |
| --- | --- | --- | --- | --- | --- |
| Holidays and time off | A request before a holiday weekend waits longer. | all human waits | Strong on those days | none (configure a calendar) | Per-person time off is not knowable. |
| Day of month, sprint or release cycle | Teams cluster review around cycles. | `wait_review` | Unknown | derive from time | Noise unless the team really works in cycles. |
| A flow-version marker | The flow changes (for example the personal review stage was added), so old tails differ. | all | Strong at a change | record | Needs a rule for when a task belongs to which flow. |
| Parked tasks | A draft left for weeks on purpose is not work time. | work stages | Strong for the tail | none (needs an explicit "parked" mark) | Excluding idle weeks hides real waiting. |
| A different finish target (ready for review, deployed) | The target is not always the merge. | the path's end | n/a | derive | A new definition, not a feature. |

### Not allowed

Anything that needs a language model or text judgement (review comment tone,
description quality, difficulty guessed from the diff). The estimate is
deterministic by decision.

## How to decide

1. Declare the feature, computed only from facts known strictly before the
   instant being predicted (a feature that uses the final size of a PR that kept
   growing, or the number of threads that arrived later, leaks the future and
   will look better in a backtest than it is in use).
2. Run the backtest with the feature in and out on the same splits. Keep it only
   if the pinball loss improves by more than the margin the champion rule uses
   (5 percent by default) over at least the minimum number of test points (30).
3. Prefer a feature that is explainable in the card's modal; a gain that cannot
   be explained is usually fitted noise.
4. Watch the samples per group: a feature with many levels (a path prefix, a
   reviewer) needs shrinkage, or it will fit a handful of tasks.
5. Re-check after a flow change: a feature that helped under the old flow may not
   under the new one.
