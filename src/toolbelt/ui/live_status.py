"""Keeps the `tt ui` page live: a background status cache and an event hub.

``StatusRefresher`` polls GitHub (through the injected ``fetch``, which is the
same lookup `tt g tree` uses) for every tracked branch and keeps the latest
result in memory, so a page render never waits on ``gh``. Whenever a branch's
status changes it calls ``on_change``; ``EventHub`` fans such events out to the
connected browsers (see the ``/events`` endpoint in ``app.py``).

Polling only runs while at least one browser is connected: the loops in
``StatusRefresher.run`` and ``StructureWatcher.run`` park on the hub until a
subscriber appears. Cost control:

* each branch is refetched once its own interval has passed (``interval``;
  ``settled_interval`` for merged/closed PRs, which rarely change), so
  branches drift apart instead of all firing together;
* at most ``concurrency`` lookups run at once, so a cold start (every branch
  due at the same moment) is spread over several rounds, not one burst.

Time and sleeping are injected so tests drive a pass at a time without waiting.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from toolbelt.git.stack.status import BranchStatus, PrState

# (branch, repo path) -> that branch's PR status.
StatusFetcher = Callable[[str, Path], Awaitable[BranchStatus]]

DEFAULT_INTERVAL_SECONDS = 20.0
DEFAULT_SETTLED_INTERVAL_SECONDS = 300.0
DEFAULT_CONCURRENCY = 4
DEFAULT_TICK_SECONDS = 2.0

_SETTLED_PR_STATES = (PrState.MERGED, PrState.CLOSED)


@dataclass(frozen=True)
class TrackedBranch:
    repo_id: int
    branch: str
    path: Path

    @property
    def key(self) -> tuple[int, str]:
        return (self.repo_id, self.branch)


@dataclass(frozen=True)
class Fetched:
    """The outcome of one lookup. ``status`` is ``None`` when it failed."""

    status: BranchStatus | None
    at: float


class EventHub:
    """Fans server events out to the connected browsers' queues.

    Everything runs on the server's single event loop, so there is no
    locking. ``version`` counts published events: a page remembers the version
    it was rendered at and a reconnecting browser compares it with the current
    one to learn whether it missed anything."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[tuple[str, dict] | None]] = set()
        self._connected = asyncio.Event()
        self.version = 0

    @property
    def subscribers(self) -> int:
        return len(self._queues)

    def subscribe(self) -> asyncio.Queue[tuple[str, dict] | None]:
        queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
        self._queues.add(queue)
        self._connected.set()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[str, dict] | None]) -> None:
        self._queues.discard(queue)
        if not self._queues:
            self._connected.clear()

    def publish(self, event: str, data: dict) -> None:
        self.version += 1
        payload = {**data, "v": self.version}
        for queue in self._queues:
            queue.put_nowait((event, payload))

    def close(self) -> None:
        """Ends every open stream (server shutdown)."""
        for queue in self._queues:
            queue.put_nowait(None)

    async def wait_for_subscriber(self) -> None:
        await self._connected.wait()


class StatusRefresher:
    def __init__(
        self,
        *,
        branches: Callable[[], list[TrackedBranch]],
        fetch: StatusFetcher,
        hub: EventHub,
        on_change: Callable[[TrackedBranch, Fetched], None],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        settled_interval: float = DEFAULT_SETTLED_INTERVAL_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        tick: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._branches = branches
        self._fetch = fetch
        self._hub = hub
        self._on_change = on_change
        self._clock = clock
        self._sleep = sleep
        self._interval = interval
        self._settled_interval = settled_interval
        self._tick = tick
        self._slots = asyncio.Semaphore(concurrency)
        self._cache: dict[tuple[int, str], Fetched] = {}
        #: When the last pass that fetched anything finished (``clock`` time).
        self.last_refreshed_at: float | None = None

    def get(self, repo_id: int, branch: str) -> Fetched | None:
        return self._cache.get((repo_id, branch))

    def seconds_since_refresh(self) -> float | None:
        if self.last_refreshed_at is None:
            return None
        return self._clock() - self.last_refreshed_at

    def _interval_for(self, fetched: Fetched) -> float:
        if fetched.status is not None and fetched.status.pr in _SETTLED_PR_STATES:
            return self._settled_interval
        return self._interval

    def _is_due(self, key: tuple[int, str], now: float) -> bool:
        fetched = self._cache.get(key)
        return fetched is None or fetched.at + self._interval_for(fetched) <= now

    async def run_once(self) -> int:
        """Fetch every branch that is due. Returns how many were fetched."""
        branches = self._branches()
        keys = {branch.key for branch in branches}
        for key in set(self._cache) - keys:
            del self._cache[key]
        now = self._clock()
        due = [branch for branch in branches if self._is_due(branch.key, now)]
        if not due:
            return 0
        await asyncio.gather(*(self._refresh(branch) for branch in due))
        self.last_refreshed_at = self._clock()
        self._hub.publish("refreshed", {})
        return len(due)

    async def _refresh(self, branch: TrackedBranch) -> None:
        async with self._slots:
            try:
                status: BranchStatus | None = await self._fetch(
                    branch.branch, branch.path
                )
            except Exception:
                # This is a background poll with nobody to propagate to: one
                # branch's failure (gh missing, bad output) is shown on its
                # row and retried, and must not stop the others.
                status = None
        previous = self._cache.get(branch.key)
        fetched = Fetched(status=status, at=self._clock())
        self._cache[branch.key] = fetched
        if previous is None or previous.status != status:
            self._on_change(branch, fetched)

    async def run(self) -> None:
        while True:
            await self._hub.wait_for_subscriber()
            await self.run_once()
            await self._sleep(self._tick)


class StructureWatcher:
    """Publishes ``structure`` when the stack DB is written by anyone else.

    ``version`` is any value that changes when the database does (see
    ``StackStore.data_version``)."""

    def __init__(
        self,
        *,
        version: Callable[[], int],
        hub: EventHub,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._version = version
        self._hub = hub
        self._sleep = sleep
        self._tick = tick
        self._seen: int | None = None

    def check(self) -> bool:
        """Compare with the last check; the first only takes the baseline."""
        current = self._version()
        if self._seen is None:
            self._seen = current
            return False
        if current == self._seen:
            return False
        self._seen = current
        self._hub.publish("structure", {})
        return True

    async def run(self) -> None:
        while True:
            await self._hub.wait_for_subscriber()
            self.check()
            await self._sleep(self._tick)
