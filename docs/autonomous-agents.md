# Autonomous agents

While `tt ui` runs, toolbelt can start an agent on a task as soon as its PR
needs one of three things an agent can do: **resolve merge conflicts**,
**resolve review comments**, or **fix failing CI**. The agent works in the
task's own worktree with the Claude Agent SDK, which uses Claude Code's login:
its spend counts against the same account limits as an interactive session.

Nothing runs until you turn it on, in two steps in the **Agents** view:

1. **Autonomous agents: on.** Every few seconds toolbelt decides, for each
   task whose next action is one of the three, whether an agent may take it,
   and why not when not. This is a *dry run*: nothing is started.
2. **Start runs.** Only then does an agent start, on the first task that may be
   taken, one run at a time.

Both switches are stored (`stacks.db`, `settings`) and off by default.

## When an agent may take a task

Checked in this order; the first that fails is the reason shown on the task's
card (with when it clears, if it clears by itself):

| Reason | |
| --- | --- |
| agents off | the first switch |
| PR not open | merged and closed PRs are left alone |
| inactive | the branch was not committed to within 3 working days (weekends do not count) |
| claimed | a person, or an interactive agent session, holds the task |
| no worktree / dirty / busy | the worktree must exist, have no uncommitted changes, and not be mid-merge or mid-rebase |
| thrash | 3 or more reviews by bots, or CI failed on 3 or more commits: left to you |
| runs per commit / per day, spend per day | per-task limits |
| global spend | the daily and monthly dollar limits for all agents |
| signed out | `claude auth status` says it is not signed in |
| screen asleep / locked | agents only run while you are at the laptop |
| another run working / back-off | only one at a time, with a pause after a task's run |

A branch an agent holds reads **Autonomous agent** instead of **Needs me** (the
action it replaced is in the tooltip). An interactive claim on the same task
wins. A run that is working when the screen locks is allowed to finish, within
its time limit.

## Limits

Defaults are conservative; override any of them in `agents:` of
`~/.config/toolbelt/config.yaml` (see `autonomous_agents/limits.py`).

| | default |
| --- | --- |
| model | `claude-haiku-5-5` (no escalation, no subagents) |
| one run | $0.25, 40 turns, 15 minutes |
| one task | 2 runs per commit, 3 per 24 hours, $0.50 per 24 hours |
| all agents | $3 per 24 hours, $30 per month (separate from the account's own limit) |
| back-off | 10 minutes after a task's run |
| one at a time | yes |

A run that reaches its dollar or turn limit ends as *hit its cap*; one that runs
past the time limit ends as *timed out* (and keeps the cost it had accrued).

## What an agent is told

`autonomous_agents/instructions.py` adapts the `pr-shepherd` skill for one
unattended PR: where a fix goes by the PR's state (critical on the branch, a
draft in place, a ready PR only if minimal, an approved PR always stacked),
run the repo's checks before every push, never force-push, resolve conflicts
with `toolbelt git sync` and never abort them, the full flaky-CI protocol, and
careful review-thread rules (bot threads may be answered and resolved; a human's
thread is never resolved, and only a clear nit is replied to). Comments start
with "🤖 AI-generated on behalf of Devon:". It ends with a one-line JSON report.

## What is recorded

* Each run is a row in `agent_runs`: the task, the trigger, the head commit,
  state, turns, the SDK's own cost and tokens, the agent's summary or the
  error. Runs still marked running when `tt ui` stops are marked abandoned.
* While it works, a run holds a claim file (`auto.<run id>`, contract v2, see
  `task-claims.md`); the **Spend** view lists autonomous spend separately, and
  an agent's session is left out of the interactive figures.
* A **Stop** button on a working run ends it as *stopped*.
