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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from toolbelt.tasks.tracker import TaskTracker
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import DEFAULT_TICK_SECONDS, EventHub
from toolbelt.ui.task_cache import TaskInfoCache


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
    ) -> None:
        self._tasks = tasks
        self._cache = cache
        self._hub = hub
        self._sleep = sleep
        self._tick = tick
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

    async def run_once(self) -> int:
        """Look up every linked task; publish those whose answer changed.
        Returns how many changed."""
        tasks = self._tasks()
        keys = {(task.repo_id, task.branch) for task in tasks}
        for key in set(self._live) - keys:
            del self._live[key]
        answers = await asyncio.gather(*(self._look_up(task) for task in tasks))
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
