"""A short in-memory TTL cache of task lookups, so a page of branches (and the
periodic refreshes) costs at most one tracker call per task per TTL."""

import threading
import time
from collections.abc import Callable

from toolbelt.tasks.tracker import TaskInfo, TaskTracker

DEFAULT_TTL_SECONDS = 60.0


class TaskInfoCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # (tracker name, ref) -> (expires_at, result); failures are cached
        # too so an unreachable tracker is not hammered.
        self._entries: dict[tuple[str, str], tuple[float, TaskInfo | None]] = {}

    def get(self, tracker: TaskTracker, ref: str) -> TaskInfo | None:
        key = (tracker.name, ref)
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None and entry[0] > self._clock():
            return entry[1]
        info = tracker.fetch(ref)
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, info)
        return info
