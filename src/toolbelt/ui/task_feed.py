"""Keeps each branch's task title and status current for the connected pages.

Like ``StatusRefresher`` (``live_status.py``) it only works while a browser is
watching: every tick it asks the ``TaskInfoCache`` for each linked task (the
cache's TTL makes that at most one tracker call per task per TTL), remembers
the latest answer and publishes a ``task`` event when one changed. The
snapshot reads the remembered answers, so rendering never waits on a tracker.

Only refs stored in the stack DB ever reach a tracker, and only through the
tracker that owns them (see ``branch_links.owning_tracker``).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from toolbelt.tasks.tracker import TaskStatusHistory, TaskTracker
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import DEFAULT_TICK_SECONDS, EventHub
from toolbelt.ui.task_cache import TaskInfoCache

STATUS_HISTORY_TTL_SECONDS = 900.0


@dataclass(frozen=True)
class LinkedTask:
    repo_id: int
    branch: str
    ref: str
    #: The tracker that owns ``ref``.
    tracker: TaskTracker


class TaskFeed:
    def __init__(
        self,
        *,
        tasks: Callable[[], list[LinkedTask]],
        cache: TaskInfoCache,
        hub: EventHub,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = DEFAULT_TICK_SECONDS,
        record_history: Callable[
            [LinkedTask, TaskStatusHistory], bool
        ] = lambda task, history: False,
        on_history_change: Callable[[], None] = lambda: None,
        history_ttl: float = STATUS_HISTORY_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tasks = tasks
        self._cache = cache
        self._hub = hub
        self._sleep = sleep
        self._tick = tick
        self._record_history = record_history
        self._on_history_change = on_history_change
        self._history_ttl = history_ttl
        self._clock = clock
        self._history_checked: dict[tuple[str, str], float] = {}
        self._live: dict[tuple[int, str], api.TaskLive] = {}

    def get(self, repo_id: int, branch: str) -> api.TaskLive:
        return self._live.get((repo_id, branch), api.TaskLoading(state="loading"))

    async def _look_up(self, task: LinkedTask) -> api.TaskLive:
        info = await asyncio.to_thread(self._cache.get, task.tracker, task.ref)
        if info is None:
            return api.TaskUnavailable(
                state="unavailable",
                reason=task.tracker.unavailable_reason() or "task details unavailable",
            )
        return api.TaskDetails(state="info", title=info.title, status=info.status)

    async def _sync_history(self, task: LinkedTask) -> bool:
        if task.tracker.name != "jira":
            return False
        key = (task.tracker.name, task.ref)
        now = self._clock()
        if now - self._history_checked.get(key, float("-inf")) < self._history_ttl:
            return False
        self._history_checked[key] = now
        history = await asyncio.to_thread(task.tracker.status_history, task.ref)
        if history is None:
            return False
        return await asyncio.to_thread(self._record_history, task, history)

    async def run_once(self) -> int:
        """Look up every linked task; publish those whose answer changed.
        Returns how many changed."""
        tasks = self._tasks()
        keys = {(task.repo_id, task.branch) for task in tasks}
        for key in set(self._live) - keys:
            del self._live[key]
        answers, history_changes = await asyncio.gather(
            asyncio.gather(*(self._look_up(task) for task in tasks)),
            asyncio.gather(*(self._sync_history(task) for task in tasks)),
        )
        if any(history_changes):
            self._on_history_change()
        changed = 0
        for task, live in zip(tasks, answers, strict=True):
            key = (task.repo_id, task.branch)
            if self._live.get(key) == live:
                continue
            self._live[key] = live
            changed += 1
            self._hub.publish(
                "task",
                {
                    "branch_id": f"{task.repo_id}:{task.branch}",
                    "task": live.model_dump(mode="json"),
                },
            )
        return changed

    async def run(self) -> None:
        while True:
            await self._hub.wait_for_subscriber()
            while self._hub.watched():
                await self.run_once()
                await self._sleep(self._tick)
