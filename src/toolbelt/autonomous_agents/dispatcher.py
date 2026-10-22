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
from toolbelt.autonomous_agents.ci_history import CiExplainer
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
from toolbelt.git.stack.status import BranchStatus, PrState, ReviewState
from toolbelt.git.stack.store import AgentRun
from toolbelt.logger import logger
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
    #: The PR, as of the last fetch: what an agent is told about it.
    pr_number: int | None = None
    pr_url: str | None = None
    base_ref: str | None = None
    approved: bool = False
    #: Where the branch is checked out (None: nowhere).
    worktree: Path | None = None
    #: A person starting a run by hand would get past ``reason``.
    can_override: bool = False

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


class OverrideRefused(Exception):
    """A run could not be started by hand: ``reason`` is still true."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.text)
        self.reason = reason


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
        launching: Callable[[], bool] = lambda: True,
        explain_ci: CiExplainer | None = None,
    ) -> None:
        self._tasks = tasks
        self._fetch = fetch
        self._holders = holders
        self._inspect = inspect
        self._runs = runs
        self._enabled = enabled
        self.limits = limits
        self._read_screen = screen
        self._check_signed_in = signed_in
        self._wall_clock = wall_clock
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._tick = tick
        self._concurrency = concurrency
        self._launch = launch
        self._launching = launching
        self._explain_ci = explain_ci
        #: Why CI keeps failing on a PR that trips the guard, by task, for the
        #: head commit it was looked up at: a new commit looks it up again.
        self._ci_history: dict[tuple[int, str], tuple[str, str | None]] = {}
        self._launching_now = False
        #: Called at the end of every pass, once the decisions are current.
        self.on_tick: Callable[[], None] | None = None
        #: Latest PR snapshot per branch and when (``clock``) it was fetched.
        self._snapshots: dict[tuple[int, str], tuple[float, PrSnapshot]] = {}
        self._auth: tuple[float, bool] | None = None
        #: The newest decisions, by (repo id, branch).
        self.decisions: dict[tuple[int, str], Decision] = {}
        #: Tasks a person asked to start a run on that has not started yet (the
        #: decision it needs takes seconds), and why one that did not start did
        #: not (shown until the next request for that task).
        self.starting: set[tuple[int, str]] = set()
        self.start_errors: dict[tuple[int, str], str] = {}
        self._starts: set[asyncio.Task[None]] = set()
        #: What stops every agent right now, as of the last tick.
        self.blockers: list[Reason] = []
        #: When the last tick ended (None: none yet).
        self.ticked_at: datetime | None = None
        #: Why the last tick failed (None: it did not).
        self.last_error: str | None = None
        #: The screen and sign-in as of the last tick (None: unknown).
        self.screen: ScreenState | None = None
        self.signed_in: bool | None = None

    @property
    def dry_run(self) -> bool:
        """Nothing starts runs: tasks are decided and no more (there is no
        launcher, or the switch for starting runs is off)."""
        return self._launch is None or not self._launching_now

    def _auth_ok(self) -> bool:
        now = self._clock()
        if self._auth is None or now - self._auth[0] >= AUTH_CHECK_SECONDS:
            self._auth = (now, self._check_signed_in())
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

    async def _look_into_ci(self, tasks: list[DispatchTask]) -> None:
        """For each PR that trips the guard against re-blocked PRs, find out why
        its CI keeps failing (once per head commit)."""
        if self._explain_ci is None:
            return
        for item in tasks:
            key = item.task.key
            snapshot = self._snapshots.get(key)
            if snapshot is None or snapshot[1].facts is None:
                continue
            facts = snapshot[1].facts
            if (
                facts.state != "OPEN"
                or failed_ci_commits(facts) < self.limits.thrash_failed_commits
                or self._ci_history.get(key, ("", None))[0] == facts.head_oid
            ):
                continue
            try:
                explained = await self._explain_ci(item.repo_path, facts.number)
            except Exception:  # noqa: BLE001 - optional: the decision stands without it
                logger.exception("looking into %s's CI failed", item.task.branch)
                explained = None
            self._ci_history[key] = (facts.head_oid, explained)

    def _world(self, now: datetime) -> World:
        enabled = self._enabled()
        return World(
            now=now,
            enabled=enabled,
            limits=self.limits,
            screen=self._read_screen(),
            # Not worth asking while agents are off.
            auth_ok=self._auth_ok() if enabled else None,
            runs=self._runs(),
        )

    async def tick(self) -> None:
        """One pass: refresh stale PR snapshots, then decide every task.
        While agents are switched off nothing is fetched or decided: the only
        thing to say is that they are off. The blocking parts (database,
        sign-in check, git) run off the event loop."""
        now = self._wall_clock()
        world = await asyncio.to_thread(self._world, now)
        self.screen = world.screen
        self.signed_in = world.auth_ok
        self._launching_now = world.enabled and self._launching()
        if not world.enabled:
            self.decisions = {}
            self.blockers = global_blockers(world)
            self.ticked_at = now
            if self.on_tick is not None:
                self.on_tick()
            return
        tasks = await asyncio.to_thread(self._tasks)
        await self._refresh(tasks)
        await self._look_into_ci(tasks)
        self.decisions = await asyncio.to_thread(self._decide_all, tasks, world)
        self.blockers = global_blockers(world)
        self.ticked_at = now
        if self._launch is not None and self._launching_now:
            # One at a time: every decision was made against the same runs, so
            # a second start in this pass would ignore the first.
            for decision in self.decisions.values():
                if decision.may_start:
                    await self._launch(decision)
                    break
        if self.on_tick is not None:
            self.on_tick()

    def _decide_all(
        self, tasks: list[DispatchTask], world: World
    ) -> dict[tuple[int, str], Decision]:
        decisions: dict[tuple[int, str], Decision] = {}
        for item in tasks:
            entry = self._snapshots.get(item.task.key)
            if entry is None:
                continue
            decision = self._decide(item, entry[1], world)
            if decision is not None:
                decisions[item.task.key] = decision
        return decisions

    def request_start(self, key: tuple[int, str]) -> None:
        """Starts a run on the task by hand, in the background: the decision it
        needs (git, the sign-in check) takes seconds, so this returns at once,
        the task reads ``starting`` until its run is recorded, and a refusal or
        failure is in ``start_errors``. Raises ``KeyError`` for a task with
        nothing an agent does."""
        if key not in self.decisions:
            raise KeyError(key)
        if key in self.starting:
            return
        self.starting.add(key)
        self.start_errors.pop(key, None)
        self._changed()
        task = asyncio.create_task(self._start_requested(key))
        self._starts.add(task)
        task.add_done_callback(self._starts.discard)

    async def _start_requested(self, key: tuple[int, str]) -> None:
        try:
            await self.start_now(key)
        except OverrideRefused as refused:
            self.start_errors[key] = refused.reason.text
        except KeyError:
            self.start_errors[key] = "There is nothing for an agent to do on this task."
        except Exception as error:  # noqa: BLE001 - shown on the task's card
            logger.exception("starting a run on %s failed", key[1])
            self.start_errors[key] = f"Could not start a run: {error}"
        finally:
            self.starting.discard(key)
            self._changed()

    def _changed(self) -> None:
        if self.on_tick is not None:
            self.on_tick()

    async def idle(self) -> None:
        """Returns when no requested start is in flight."""
        while self._starts:
            await asyncio.gather(*self._starts)

    async def start_now(self, key: tuple[int, str]) -> None:
        """Start a run on the task by hand, past the reasons a person may
        override (``eligibility.OVERRIDABLE``), against the latest PR snapshot.
        Raises ``KeyError`` for a task with nothing an
        agent does, and ``OverrideRefused`` when a reason that stays is true."""
        if self._launch is None:
            raise OverrideRefused(Reason("disabled", "Nothing can start runs here."))
        entry = self._snapshots.get(key)
        if entry is None:
            raise KeyError(key)
        now = self._wall_clock()
        world = await asyncio.to_thread(self._world, now)
        tasks = await asyncio.to_thread(self._tasks)
        item = next((t for t in tasks if t.task.key == key), None)
        decision = (
            await asyncio.to_thread(self._decide, item, entry[1], world, True)
            if item
            else None
        )
        if decision is None:
            raise KeyError(key)
        if decision.reason is not None:
            raise OverrideRefused(decision.reason)
        await self._launch(decision)

    def _ci_history_of(self, key: tuple[int, str], snapshot: PrSnapshot) -> str | None:
        """Why CI keeps failing, if it was looked into at the PR's current head."""
        held = self._ci_history.get(key)
        if held is None or snapshot.facts is None or held[0] != snapshot.facts.head_oid:
            return None
        return held[1]

    def _decide(
        self,
        item: DispatchTask,
        snapshot: PrSnapshot,
        world: World,
        override: bool = False,
    ) -> Decision | None:
        # The action the branch has on its own, whoever holds it.
        action = next_action(snapshot.status, None)
        if not is_trigger(action.kind):
            return None
        task = item.task
        state = self._inspect(item)
        if not state.exists:
            return Decision(
                task_id=task.task_id,
                repo_id=task.repo_id,
                repo=task.repo_name,
                branch=task.branch,
                trigger=action.kind,
                reason=Reason(
                    "no_worktree",
                    "There is no local branch of this name to work on.",
                ),
                head_sha="",
                decided_at=world.now,
            )
        holders = self._holders(task.repo_id, task.branch)
        facts = TaskFacts(
            task_id=task.task_id,
            action_kind=action.kind,
            pr_open=pr_is_open(snapshot.status),
            pr_draft=snapshot.status.pr is PrState.DRAFT,
            head_sha=state.head_sha,
            last_commit_at=state.last_commit_at,
            has_worktree=task.worktree is not None,
            worktree_clean=state.clean,
            worktree_busy=state.busy,
            claimed_by_other=any(h.kind != "autonomous" for h in holders),
            bot_reviews=bot_reviews(snapshot.facts),
            failed_ci_commits=failed_ci_commits(snapshot.facts),
            ci_history=self._ci_history_of(item.task.key, snapshot),
        )
        reason = decide(facts, world, override=override)
        return Decision(
            task_id=task.task_id,
            repo_id=task.repo_id,
            repo=task.repo_name,
            branch=task.branch,
            trigger=action.kind,
            reason=reason,
            can_override=reason is not None
            and decide(facts, world, override=True) is None,
            head_sha=state.head_sha,
            decided_at=world.now,
            pr_number=snapshot.facts.number if snapshot.facts else None,
            pr_url=snapshot.status.url,
            base_ref=snapshot.facts.base_ref if snapshot.facts else None,
            approved=snapshot.status.review is ReviewState.APPROVED,
            worktree=task.worktree,
        )

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                self.last_error = None
            except Exception as error:  # noqa: BLE001 - a background loop must survive
                # GitHub, git and the claude CLI can all fail: say so, and
                # try again next tick instead of ending the dispatcher.
                logger.exception("the agent dispatcher's pass failed")
                self.last_error = f"{type(error).__name__}: {error}"
            await self._sleep(self._tick)
