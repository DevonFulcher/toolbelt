"""When an autonomous agent may pick a task up, and when it may not and why.

Pure functions over what the dispatcher has gathered, so every reason is
testable and the UI can show the same ``Reason`` the dispatcher acted on.
``decide`` gives the one reason that matters most for a task, checked in the
order of ``decide``'s body: whether agents are on at all, then what is true of
the task for a while (its PR and whether it is still a draft, how old its last commit is, who holds it, its
worktree, thrash, its run and spend limits), then what is true of all agents
(spend limits, sign-in, the screen), then what passes soon (another run, a
back-off).
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from toolbelt.autonomous_agents.limits import TRIGGERS, AgentLimits
from toolbelt.autonomous_agents.screen import ScreenState
from toolbelt.git.stack.store import AgentRun

type ReasonCode = Literal[
    "disabled",
    "pr_not_open",
    "draft",
    "inactive",
    "claimed",
    "no_worktree",
    "worktree_dirty",
    "worktree_busy",
    "thrash",
    "runs_per_head",
    "task_runs_per_day",
    "task_usd_per_day",
    "global_usd_per_day",
    "global_usd_per_month",
    "auth",
    "screen_asleep",
    "screen_locked",
    "concurrency",
    "backoff",
]


#: A reason an agent is not on a task: ``until`` is when it stops being true
#: by itself (None: it does not, or only a change elsewhere ends it).
@dataclass(frozen=True)
class Reason:
    code: ReasonCode
    text: str
    until: datetime | None = None


@dataclass(frozen=True)
class TaskFacts:
    """What the dispatcher knows about one task's branch."""

    task_id: str
    #: The next action the branch would have without any claim (a ``kind`` of
    #: ``next_action``).
    action_kind: str
    pr_open: bool
    #: The open PR is still a draft.
    pr_draft: bool
    head_sha: str
    last_commit_at: datetime | None
    has_worktree: bool
    worktree_clean: bool
    #: In the middle of a merge, rebase, cherry-pick or revert.
    worktree_busy: bool
    #: A person, or an interactive agent session, holds the task.
    claimed_by_other: bool
    #: Reviews by bots on the PR, and commits whose CI failed.
    bot_reviews: int
    failed_ci_commits: int


@dataclass(frozen=True)
class World:
    """What is true of all agents right now."""

    now: datetime
    enabled: bool
    limits: AgentLimits
    #: None when the screen could not be read.
    screen: ScreenState | None
    #: None while sign-in has not been checked.
    auth_ok: bool | None
    #: Every run started this month (UTC) or in the last 24 hours, whichever
    #: reaches back further.
    runs: Sequence[AgentRun]


def is_trigger(action_kind: str) -> bool:
    return action_kind in TRIGGERS


def working_days_between(start: date, end: date) -> int:
    """How many weekdays (Monday to Friday) lie after ``start`` up to and
    including ``end``."""
    days = 0
    day = start
    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days += 1
    return days


def _since(runs: Sequence[AgentRun], start: datetime) -> list[AgentRun]:
    return [run for run in runs if run.started_at >= start]


def month_start(now: datetime) -> datetime:
    utc = now.astimezone(UTC)
    return datetime(utc.year, utc.month, 1, tzinfo=UTC)


def spend_since(runs: Sequence[AgentRun], start: datetime) -> float:
    return sum(run.cost_usd for run in _since(runs, start))


def global_blockers(world: World) -> list[Reason]:
    """What stops every agent right now (for the page's banner), most
    important first."""
    limits = world.limits
    day_ago = world.now - timedelta(hours=24)
    blockers: list[Reason] = []
    if not world.enabled:
        blockers.append(Reason("disabled", "Autonomous agents are switched off."))
    day = spend_since(world.runs, day_ago)
    if day >= limits.global_usd_per_day:
        oldest = min((r.started_at for r in _since(world.runs, day_ago)), default=None)
        blockers.append(
            Reason(
                "global_usd_per_day",
                f"Agents have spent ${day:.2f} of their ${limits.global_usd_per_day:.2f} "
                "limit for the last 24 hours.",
                until=oldest + timedelta(hours=24) if oldest else None,
            )
        )
    month = spend_since(world.runs, month_start(world.now))
    if month >= limits.global_usd_per_month:
        year, number = divmod(month_start(world.now).month, 12)
        resume = datetime(month_start(world.now).year + year, number + 1, 1, tzinfo=UTC)
        blockers.append(
            Reason(
                "global_usd_per_month",
                f"Agents have spent ${month:.2f} of their "
                f"${limits.global_usd_per_month:.2f} limit for this month.",
                until=resume,
            )
        )
    if world.auth_ok is False:
        blockers.append(
            Reason("auth", "Claude is not signed in: run `claude auth login`.")
        )
    if world.screen is not None and not world.screen.awake:
        blockers.append(Reason("screen_asleep", "The screen is asleep."))
    elif world.screen is not None and world.screen.locked:
        blockers.append(Reason("screen_locked", "The screen is locked."))
    return blockers


#: Reasons a person can override by starting a run by hand: what the limits,
#: the schedule and the agents' own rules hold back. What is unsafe or
#: impossible stays: a closed PR, someone else holding the task or its
#: worktree, a dirty or mid-merge worktree, the spend caps, sign-in, and the
#: run limit.
OVERRIDABLE: frozenset[ReasonCode] = frozenset(
    {
        "draft",
        "inactive",
        "thrash",
        "runs_per_head",
        "task_runs_per_day",
        "task_usd_per_day",
        "screen_asleep",
        "screen_locked",
        "backoff",
    }
)


def _reasons(task: TaskFacts, world: World) -> Iterator[Reason]:
    """Every reason no agent is working on ``task`` now, most important first.
    Only meaningful for a task whose ``action_kind`` is a trigger."""
    limits = world.limits
    now = world.now
    if not world.enabled:
        yield Reason("disabled", "Autonomous agents are switched off.")
        return
    if not task.pr_open:
        yield Reason("pr_not_open", "The PR is not open.")
        return
    if task.pr_draft:
        yield Reason("draft", "The PR is a draft: agents leave drafts to you.")
    if task.last_commit_at is None:
        yield Reason("inactive", "No commit on this branch to go by.")
    else:
        elapsed = working_days_between(
            task.last_commit_at.astimezone().date(), now.astimezone().date()
        )
        if elapsed > limits.active_working_days:
            yield Reason(
                "inactive",
                f"Last committed to {elapsed} working days ago: agents only take "
                f"tasks committed to within {limits.active_working_days}.",
            )
    if task.claimed_by_other:
        yield Reason("claimed", "A person or an agent session holds this task.")
    if not task.has_worktree:
        yield Reason("no_worktree", "The branch has no worktree to work in.")
    elif task.worktree_busy:
        yield Reason(
            "worktree_busy", "The worktree is in the middle of a merge or rebase."
        )
    elif not task.worktree_clean:
        yield Reason("worktree_dirty", "The worktree has uncommitted changes.")
    if (
        task.bot_reviews >= limits.thrash_bot_reviews
        or task.failed_ci_commits >= limits.thrash_failed_commits
    ):
        yield Reason(
            "thrash",
            f"This PR keeps getting re-blocked ({task.bot_reviews} bot reviews, "
            f"CI failed on {task.failed_ci_commits} commits): more patching may "
            "not converge, so it is left to you.",
        )
    mine = [run for run in world.runs if run.task_id == task.task_id]
    on_head = [run for run in mine if run.head_sha == task.head_sha]
    if len(on_head) >= limits.task_runs_per_head:
        yield Reason(
            "runs_per_head",
            f"{len(on_head)} runs already tried this commit "
            f"(limit {limits.task_runs_per_head}); a new commit resets it.",
        )
    today = _since(mine, now - timedelta(hours=24))
    if len(today) >= limits.task_runs_per_day:
        yield Reason(
            "task_runs_per_day",
            f"{len(today)} runs on this task in the last 24 hours "
            f"(limit {limits.task_runs_per_day}).",
            until=min(run.started_at for run in today) + timedelta(hours=24),
        )
    task_spend = sum(run.cost_usd for run in today)
    if task_spend >= limits.task_usd_per_day:
        yield Reason(
            "task_usd_per_day",
            f"${task_spend:.2f} spent on this task in the last 24 hours "
            f"(limit ${limits.task_usd_per_day:.2f}).",
            until=min(run.started_at for run in today) + timedelta(hours=24),
        )
    yield from (b for b in global_blockers(world) if b.code != "disabled")
    running = [run for run in world.runs if run.state == "running"]
    if len(running) >= limits.max_concurrent_runs:
        yield Reason(
            "concurrency",
            f"{len(running)} agent run is working already (limit "
            f"{limits.max_concurrent_runs}).",
        )
    ended = [run.ended_at for run in mine if run.ended_at is not None]
    if ended:
        resume = max(ended) + timedelta(seconds=limits.backoff_seconds)
        if resume > now:
            yield Reason(
                "backoff",
                "The last run on this task ended recently; waiting before "
                "trying again.",
                until=resume,
            )


def decide(task: TaskFacts, world: World, *, override: bool = False) -> Reason | None:
    """Why no agent is working on ``task`` now, or ``None`` when one may start.
    With ``override`` (a person starting a run by hand) the reasons in
    ``OVERRIDABLE`` do not count."""
    reasons = _reasons(task, world)
    if override:
        return next((r for r in reasons if r.code not in OVERRIDABLE), None)
    return next(reasons, None)
