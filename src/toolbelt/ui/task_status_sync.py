"""Moves a task to its in-review status once its branch's PR is open.

``tt ui`` reads every tracked branch's PR. When one is open and not a draft,
its task (if the configured tracker owns it) is moved to the tracker's
``in_review`` status: see ``toolbelt.tasks.jira`` for when a task is left where
it is (never moved back from a later status). Each PR moves its task once,
recorded in the stack DB, so a task a person moves back afterwards stays where
they put it, and a restart does not ask the tracker about every PR again.

Best-effort, like the other status moves: a task the tracker cannot reach or
change is logged and tried again at the next restart, never an error.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from toolbelt.git.stack.status import PrState
from toolbelt.tasks.tracker import Stage, TaskTracker
from toolbelt.ui.live_status import Fetched, TrackedBranch

LOGGER = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 60.0

STAGE: Stage = "in_review"

#: A branch's task ref and the tracker that owns it.
type LinkedTracker = tuple[str, TaskTracker]


class TaskStatusSync:
    def __init__(
        self,
        *,
        branches: Callable[[], list[TrackedBranch]],
        fetched: Callable[[int, str], Fetched | None],
        linked: Callable[[int, str], LinkedTracker | None],
        moved: Callable[[int, str, str, int], bool],
        record: Callable[[int, str, str, int, datetime], None],
        wall_clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        """``linked(repo_id, branch)`` is the branch's task and its tracker
        (``None``: it has none); ``moved(...)``/``record(...)`` read and write
        which PRs have had their task moved."""
        self._branches = branches
        self._fetched = fetched
        self._linked = linked
        self._moved = moved
        self._record = record
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._tick = tick
        # PRs looked at in this run: each is asked about once, not every tick.
        self._seen: set[tuple[int, str, int]] = set()

    async def check(self) -> None:
        """One sweep over the tracked branches."""
        for branch in self._branches():
            looked_up = self._fetched(branch.repo_id, branch.branch)
            if (
                looked_up is None
                or looked_up.status is None
                or looked_up.facts is None
                or looked_up.status.pr is not PrState.OPEN
            ):
                continue
            key = (branch.repo_id, branch.branch, looked_up.facts.number)
            if key in self._seen:
                continue
            self._seen.add(key)
            # The tracker is a blocking network call: off the event loop.
            await asyncio.to_thread(self._move, key)

    def _move(self, key: tuple[int, str, int]) -> None:
        repo_id, branch, number = key
        if self._moved(repo_id, branch, STAGE, number):
            return
        link = self._linked(repo_id, branch)
        if link is None:
            return
        task, tracker = link
        try:
            result = tracker.move(task, STAGE)
        except Exception:  # a tracker that raises must not stop the sweep
            LOGGER.exception("moving %s to in review failed", task)
            return
        if result.outcome == "failed":
            LOGGER.warning("Task status not updated: %s", result.message)
            return
        LOGGER.info("Task status: %s", result.message)
        self._record(repo_id, branch, STAGE, number, self._wall_clock())

    async def run(self) -> None:
        """Sweeps every ``tick`` seconds."""
        while True:
            await self.check()
            await self._sleep(self._tick)
