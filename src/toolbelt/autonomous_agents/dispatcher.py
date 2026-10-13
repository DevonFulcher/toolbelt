"""Decides, for every tracked task, whether an autonomous agent should be
working on it, and why not when it should not.

Every tick it looks at each tracked branch's PR (refetched from GitHub every
``poll_interval``, whether or not a browser is open: an agent has to notice a
conflict or a failed check without anyone looking) and, for the ones whose next
action is something an agent does, gathers what git and the claims say and
applies ``eligibility.decide``. The decisions are what the Agents view shows.

The dispatcher only decides. Starting a run is ``launch``'s job (None: a dry
run, the decision "may start" is recorded and nothing happens).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from toolbelt.autonomous_agents.branch_state import BranchState
from toolbelt.autonomous_agents.eligibility import (
    Reason,
    TaskFacts,
    World,
    decide,
    global_blockers,
    is_trigger,
)
from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.autonomous_agents.screen import ScreenState
from toolbelt.git.stack.status import BranchStatus, PrState
from toolbelt.git.stack.store import AgentRun
from toolbelt.task_claims.resolution import TrackedTask
from toolbelt.ui.claims_feed import BranchHolder
from toolbelt.ui.live_status import StatusFetcher
from toolbelt.ui.next_action import next_action
from toolbelt.ui.pr_activity import CI_FAILED_ROLLUPS, COMMIT, REVIEW, PrFacts
from toolbelt.ui.pr_fetch import PrSnapshot

DEFAULT_POLL_INTERVAL_SECONDS = 300.0
DEFAULT_TICK_SECONDS = 15.0
DEFAULT_CONCURRENCY = 4
#: How long a sign-in check is trusted.
AUTH_CHECK_SECONDS = 300.0


@dataclass(frozen=True)
class DispatchTask:
    """A tracked task and where its repo is checked out."""

    task: TrackedTask
    repo_path: Path


@dataclass(frozen=True)
class Decision:
    """What the dispatcher concluded about one task."""

    task_id: str
    repo_id: int
    repo: str
    branch: str
    #: The next action an agent would do (``fix_ci``, ``resolve_conflicts``,
    #: ``resolve_comments``). Decisions exist only for tasks that have one.
    trigger: str
    #: Why no agent is on it; None: one may start.
    reason: Reason | None
    #: The branch's head when decided (what a run would be launched on).
    head_sha: str
    decided_at: datetime

    @property
    def may_start(self) -> bool:
        return self.reason is None


def _is_bot(login: str) -> bool:
    folded = login.casefold()
    return folded.endswith("[bot]") or "copilot" in folded


def bot_reviews(facts: PrFacts | None) -> int:
    """Reviews on the PR by bots (Copilot and similar)."""
    if facts is None:
        return 0
    return sum(1 for item in facts.items if item.kind == REVIEW and _is_bot(item.actor))


def failed_ci_commits(facts: PrFacts | None) -> int:
    """Distinct commits of the PR whose CI failed."""
    if facts is None:
        return 0
    return len(
        {
            item.oid
            for item in facts.items
            if item.kind == COMMIT and item.ci in CI_FAILED_ROLLUPS
        }
    )


def pr_is_open(status: BranchStatus) -> bool:
    return status.pr in (PrState.OPEN, PrState.DRAFT)


class Dispatcher:
    def __init__(
        self,
        *,
        tasks: Callable[[], list[DispatchTask]],
        fetch: StatusFetcher,
        holders: Callable[[int, str], tuple[BranchHolder, ...]],
        inspect: Callable[[DispatchTask], BranchState],
        runs: Callable[[], list[AgentRun]],
        enabled: Callable[[], bool],
        limits: AgentLimits,
        screen: Callable[[], ScreenState | None],
        signed_in: Callable[[], bool],
        wall_clock: Callable[[], datetime],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        tick: float = DEFAULT_TICK_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        launch: Callable[[Decision], Awaitable[None]] | None = None,
    ) -> None:
        self._tasks = tasks
        self._fetch = fetch
        self._holders = holders
        self._inspect = inspect
        self._runs = runs
        self._enabled = enabled
        self.limits = limits
        self._screen = screen
        self._signed_in = signed_in
        self._wall_clock = wall_clock
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._tick = tick
        self._concurrency = concurrency
        self._launch = launch
        #: Latest PR snapshot per branch and when (``clock``) it was fetched.
        self._snapshots: dict[tuple[int, str], tuple[float, PrSnapshot]] = {}
        self._auth: tuple[float, bool] | None = None
        #: The newest decisions, by (repo id, branch).
        self.decisions: dict[tuple[int, str], Decision] = {}
        #: What stops every agent right now, as of the last tick.
        self.blockers: list[Reason] = []
        #: When the last tick ended (None: none yet).
        self.ticked_at: datetime | None = None

    def _auth_ok(self) -> bool:
        now = self._clock()
        if self._auth is None or now - self._auth[0] >= AUTH_CHECK_SECONDS:
            self._auth = (now, self._signed_in())
        return self._auth[1]

    async def _refresh(self, tasks: list[DispatchTask]) -> None:
        """Fetch the snapshots that are older than the poll interval."""
        now = self._clock()
        gate = asyncio.Semaphore(self._concurrency)

        async def one(item: DispatchTask) -> None:
            key = item.task.key
            async with gate:
                snapshot = await self._fetch(item.task.branch, item.repo_path)
            self._snapshots[key] = (self._clock(), snapshot)

        due = [
            item
            for item in tasks
            if item.task.key not in self._snapshots
            or now - self._snapshots[item.task.key][0] >= self._poll_interval
        ]
        await asyncio.gather(*(one(item) for item in due))

    def _world(self, now: datetime) -> World:
        enabled = self._enabled()
        return World(
            now=now,
            enabled=enabled,
            limits=self.limits,
            screen=self._screen(),
            # Not worth asking while agents are off.
            auth_ok=self._auth_ok() if enabled else None,
            runs=self._runs(),
        )

    async def tick(self) -> None:
        """One pass: refresh stale PR snapshots, then decide every task.
        While agents are switched off nothing is fetched or decided: the only
        thing to say is that they are off."""
        now = self._wall_clock()
        world = self._world(now)
        if not world.enabled:
            self.decisions = {}
            self.blockers = global_blockers(world)
            self.ticked_at = now
            return
        tasks = self._tasks()
        await self._refresh(tasks)
        decisions: dict[tuple[int, str], Decision] = {}
        for item in tasks:
            entry = self._snapshots.get(item.task.key)
            if entry is None:
                continue
            decision = self._decide(item, entry[1], world)
            if decision is not None:
                decisions[item.task.key] = decision
        self.decisions = decisions
        self.blockers = global_blockers(world)
        self.ticked_at = now
        if self._launch is not None:
            for decision in decisions.values():
                if decision.may_start:
                    await self._launch(decision)

    def _decide(
        self, item: DispatchTask, snapshot: PrSnapshot, world: World
    ) -> Decision | None:
        # The action the branch has on its own, whoever holds it.
        action = next_action(snapshot.status, None)
        if not is_trigger(action.kind):
            return None
        task = item.task
        state = self._inspect(item)
        holders = self._holders(task.repo_id, task.branch)
        facts = TaskFacts(
            task_id=task.task_id,
            action_kind=action.kind,
            pr_open=pr_is_open(snapshot.status),
            head_sha=state.head_sha,
            last_commit_at=state.last_commit_at,
            has_worktree=task.worktree is not None,
            worktree_clean=state.clean,
            worktree_busy=state.busy,
            claimed_by_other=any(h.kind != "autonomous" for h in holders),
            bot_reviews=bot_reviews(snapshot.facts),
            failed_ci_commits=failed_ci_commits(snapshot.facts),
        )
        return Decision(
            task_id=task.task_id,
            repo_id=task.repo_id,
            repo=task.repo_name,
            branch=task.branch,
            trigger=action.kind,
            reason=decide(facts, world),
            head_sha=state.head_sha,
            decided_at=world.now,
        )

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._sleep(self._tick)
