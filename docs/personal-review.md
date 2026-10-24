# Validate, personal review, publish

A task (a tracked branch) goes from "work done" to "PR open for review" in three
button presses. Nothing here is inferred or automatic: you decide when a branch
is ready, and no PR or CI is involved until you approve.

```
Open PR  --Validate-->  (agent run)  -->  Personal review  --Approve-->  PR open
   ^                                           |
   +------------- Request changes -------------+
```

## 1. Validate

The **Validate** button is in the card's *Autonomous agent* slot while the task
has no PR. It starts an autonomous agent run by hand, on the task's worktree.
The run does what the `validate` skill does: a production-quality review of the
branch's diff against its parent (`git diff <parent>...HEAD`) across code
quality, performance, security, maintainability, tests and operations, ending
with a numbered list of issues and whether it would approve. It is read-only: it
may run the tests, but it must not edit, commit or push, and it is not given the
`Edit` and `Write` tools.

`validate` is its own job, not a next action (`limits.TRIGGERS` is unchanged;
`limits.VALIDATE`), so the dispatcher never starts it. Its rules are
`eligibility.validate_blocker`, not the PR-driven ones:

| Reason | |
| --- | --- |
| has PR | the task already has a PR (any state): validate runs before one |
| working | a run is already on the task |
| deciding | an agent asked a question about the task: answer it first |
| claimed | a person, or an interactive agent session, holds the task |
| no worktree / dirty / busy | the worktree must exist, have no uncommitted changes, and not be mid-merge or mid-rebase |
| task spend / runs per day | `task_usd_per_day`, `task_runs_per_day` |
| global spend, signed out | the same limits as every agent |

It ignores the "Autonomous agents" and "Start runs" switches, the screen, how old
the branch is, and the back-off: you pressed the button. The run is recorded in
`agent_runs` with trigger `validate`, uses the per-run dollar, turn and time
limits of [autonomous agents](autonomous-agents.md), and ends with a **result**
(`agent_runs.result`): `pass` when the agent would approve, `fail` when it would
ask for changes (from the `verdict` of its final JSON line). A refusal appears
on the card as the run's `start_error`. A run that asks you a question ends as
*waiting for you* and is resumed with the answer like any other
([decisions](decisions.md); resuming needs the agents switches on).

## 2. Personal review

When a validate run **succeeds** (not failed, timed out, capped or stopped) and
no question is pending, the task's Next becomes **Personal review** (kind
`personal_review`, who: you), replacing **Open PR**. A pending question still
reads **Deciding** first. The pass/fail result does not decide this: a `fail`
means the agent would ask for changes, and you read its summary and decide.

The card's Next is a link to the **Review** view of the branch against its
parent. There, above the diff, the personal review bar shows the run's result and
summary and offers:

- **Approve and open PR**, with an optional note and a **Post in Slack once it is
  open** box (shown only when Slack is configured; off by default, see below).
- **Request changes**, which needs a note. The task reads **Open PR** again until
  another validate run succeeds.

Both are stored in `review_verdicts` (task, repo, branch, **head commit**,
verdict, note, time). The page sends the commit it showed; if the branch has a
new commit since, the verdict is refused ("reload it"). An approval stands only
for its commit: a new commit makes `current_verdict` forget it. Line-level
comments are not supported.

The task stays in personal review until its PR exists; Jira stays "In Progress".

## 3. Publish

Approving opens the PR for review in the background (the Next reads *Opening
PR…*, the outcome comes through the live stream, a failure is the card's
notice, and approving again retries). `personal_review.open_pr`:

1. `git push -u origin <branch>` (never forced);
2. `gh pr create --base <parent> --head <branch> --fill` (not a draft).

The PR's lookup is refreshed at once, so the card moves on to the PR's own
rules (`Post in Slack`, `Request review`, ...). The PR link is recorded in the
stack database by the next sync, as for any PR.

**Slack.** By default the PR then reads **Post in Slack**, with the dialog that
shows the message for you to confirm, exactly as in [slack posts](slack-posts.md).
With *Post in Slack once it is open* ticked, toolbelt drafts the same message
and posts it straight away, as if you had pressed Post in that dialog. If Slack
is not configured or the post fails, the PR is still open and the card says so.

## What is recorded

| Input | Recorded in | Read by |
| --- | --- | --- |
| a validate run and its result | `agent_runs` (`trigger = 'validate'`, `result`) | `personal_review.validation_spans` |
| your verdict | `review_verdicts` | the same, and `current_verdict` |

A task is *validated* from the end of a succeeded validate run until a request
for changes. That is the `personal_review` lane (a span, *Validated*, plus an
*approved* / *changes requested* marker each), which `aggregate.task_segments`
replays so the aggregate view and the card's "next" lane show Personal review
too ([timeline lanes](timeline-lanes.md)). The agent lane's span for a validate
run carries its result in the tooltip.

## Not included

Triggering validation automatically, inferring that a branch is ready,
line-level review comments, and CI.
