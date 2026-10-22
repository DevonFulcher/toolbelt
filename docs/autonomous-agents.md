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
| working | a run is already on the task: it shows under "An agent is on these", whatever else its run changes (a dirty worktree) |
| draft | agents only work on PRs that are ready for review; a draft is left to you, and sits under "No agent is on these" |
| inactive | the branch was not committed to within 3 working days (weekends do not count) |
| claimed | a person, or an interactive agent session, holds the task |
| no worktree / dirty / busy | the worktree must exist, have no uncommitted changes, and not be mid-merge or mid-rebase |
| thrash | 3 or more reviews by bots, or CI failed on 3 or more commits: left to you |
| runs per commit / per day, spend per day | per-task limits |
| global spend | the daily and monthly dollar limits for all agents |
| signed out | `claude auth status` says it is not signed in |
| screen asleep / locked | agents only run while you are at the laptop |
| another run working / back-off | only one at a time, with a pause after a task's run |

### Running one by hand

With "Start runs" off, a task nothing holds back reads "An agent could take this, but Start runs is off" and has a **Run an agent now** button.

A held-back task whose reason is one of draft, inactive, thrash, runs per
commit / per day, spend per task per day, screen asleep / locked or back-off has
a **Run an agent anyway** button. It starts a run on that task right away, even
with "Start runs" off, and the run counts toward the limits like any other.
Everything else stays: a closed PR, a task someone holds, a missing, dirty or
mid-merge worktree, the global spend limits, signed out, and the one run at a
time. If one of those is true the button says which.

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
| one task | 2 runs of the same job per commit, 3 per 24 hours, $0.50 per 24 hours |
| all agents | $3 per 24 hours, $30 per month (separate from the account's own limit) |
| back-off | 10 minutes after a task's run |
| one at a time | yes |

A run that reaches its dollar or turn limit ends as *hit its cap*; one that runs
past the time limit ends as *timed out* (and keeps the cost it had accrued).

## What an agent is told

`autonomous_agents/instructions.py` adapts the `pr-shepherd` skill for one
unattended PR: where a fix goes by the PR's state (critical on the branch, a
ready PR only if minimal, an approved PR always stacked; agents never get a
draft),
commit and push only with `toolbelt git save` (never plain git: it syncs the
stack, and a case toolbelt cannot handle is reported in `needs_human`, not
worked around), run the repo's checks before every save, never force-push, resolve conflicts
with `toolbelt git sync` and never abort them, the full flaky-CI protocol, and
careful review-thread rules (bot threads may be answered and resolved; a human's
thread is never resolved, and only a clear nit is replied to). A thread counts as
a bot's when a bot account started it, or when its first comment begins by saying
an AI wrote it under a person's account (for example "**Codex (AI) review:**" or
"🤖 AI-generated"): the card counts those as bot comments and the agent treats
them as its work. Comments start
with "🤖 AI-generated on behalf of Devon:". It ends with a one-line JSON report.

## What is recorded

* Each run is a row in `agent_runs`: the task, the trigger, the head commit,
  state, turns, the SDK's own cost and tokens, the agent's summary or the
  error. Runs still marked running when `tt ui` stops are marked abandoned.
* While it works, a run holds a claim file (`auto.<run id>`, contract v2, see
  `task-claims.md`); the **Spend** view lists autonomous spend separately, and
  an agent's session is left out of the interactive figures.
* A **Stop** button on a working run ends it as *stopped*.
* A **Conversation** button on a run shows what it was asked, what the agent
  said, and each tool it ran with the output (long output is cut). It is read
  from the run's own Claude session transcript, so it fills in live while the
  run works.
* On the task card, an **Agent** group holds the branch's interactive session
  and the autonomous agents' view of it: what an agent would do or why none is
  on it (with Run an agent now / anyway and Stop buttons), and the latest run
  with its conversation. A **Spend** group shows what the task has cost:
  interactive (estimated from transcripts) and autonomous (each run's own
  cost). The Agents view keeps the log of every run.
