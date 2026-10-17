"""Keeps the `tt ui` page live: a background status cache and an event hub.

``StatusRefresher`` polls GitHub (through the injected ``fetch``, which is the
same lookup `tt g tree` uses) for every tracked branch and keeps the latest
result in memory, so a page render never waits on ``gh``. Whenever a branch's
status changes it calls ``on_change``; ``EventHub`` fans such events out to the
connected browsers (see the ``/api/events`` endpoint in ``app.py``).

Polling only runs while at least one browser is connected: the loops in
``StatusRefresher.run`` and ``StructureWatcher.run`` park on the hub until a
subscriber appears. A browser that is open but not being looked at (hidden
tab, minimized window) closes its stream (see ``web/src/live/connection.ts``), so
that also stops the polling. When the last browser leaves, polling carries on
for ``EventHub``'s grace period, so a page reload or a dropped connection does
not stop and restart it; when one arrives, the refresher wakes at once and
refetches everything stale (older than its interval) instead of waiting out its
tick. Cost control:

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
from datetime import UTC, datetime
from pathlib import Path

from toolbelt.git.stack.status import BranchStatus, PrState
from toolbelt.ui.conflict_commit import ConflictCommit
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.pr_fetch import PrSnapshot

# (branch, repo path) -> that branch's PR status and history.
StatusFetcher = Callable[[str, Path], Awaitable[PrSnapshot]]

DEFAULT_INTERVAL_SECONDS = 20.0
DEFAULT_SETTLED_INTERVAL_SECONDS = 300.0
DEFAULT_CONCURRENCY = 4
DEFAULT_TICK_SECONDS = 2.0
DEFAULT_GRACE_SECONDS = 5.0

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
    """The outcome of one lookup. ``status`` is ``None`` when it failed;
    ``facts`` is the PR's history (``None`` too when the branch has no PR)."""

    status: BranchStatus | None
    at: float
    facts: PrFacts | None = None
    #: Where the branch's conflict comes from, when it has one.
    conflict: ConflictCommit | None = None
    #: When (wall clock) the lookup finished: what a card says its data is as
    #: old as.
    wall_at: datetime | None = None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class EventHub:
    """Fans server events out to the connected browsers' queues.

    Everything runs on the server's single event loop, so there is no
    locking. ``version`` counts published events: a page remembers the version
    it was rendered at and a reconnecting browser compares it with the current
    one to learn whether it missed anything.

    ``grace`` is how long (``clock`` seconds) the hub still counts as watched
    after its last subscriber left."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        grace: float = 0.0,
    ) -> None:
        self._queues: set[asyncio.Queue[tuple[str, dict] | None]] = set()
        self._connected = asyncio.Event()
        self._arrived = asyncio.Event()
        self._clock = clock
        self._grace = grace
        self._last_left: float | None = None
        self.version = 0

    @property
    def subscribers(self) -> int:
        return len(self._queues)

    def subscribe(self) -> asyncio.Queue[tuple[str, dict] | None]:
        queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
        self._queues.add(queue)
        self._connected.set()
        self._arrived.set()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[str, dict] | None]) -> None:
        self._queues.discard(queue)
        if not self._queues:
            self._connected.clear()
            self._last_left = self._clock()

    def watched(self) -> bool:
        """Whether someone is connected, or left less than ``grace`` ago."""
        if self._queues:
            return True
        return (
            self._last_left is not None
            and self._clock() - self._last_left < self._grace
        )

    def clear_arrival(self) -> None:
        self._arrived.clear()

    async def wait_for_arrival(self) -> None:
        """Returns once a subscriber has connected since ``clear_arrival``."""
        await self._arrived.wait()

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
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        branch_id: Callable[[int, str], str] = lambda repo_id, name: (
            f"{repo_id}:{name}"
        ),
    ) -> None:
        self._wall_clock = wall_clock
        self._branch_id = branch_id
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
        fetched = await asyncio.gather(*(self._refresh(branch) for branch in due))
        self.last_refreshed_at = self._clock()
        # Which branches were looked up and when, including those whose answer
        # did not change: their cards say how old their data is.
        self._hub.publish(
            "refreshed",
            {
                "fetched": [
                    {
                        "branch_id": self._branch_id(branch.repo_id, branch.branch),
                        "fetched_at": _iso(result.wall_at),
                    }
                    for branch, result in fetched
                    if result.status is not None and result.wall_at is not None
                ]
            },
        )
        return len(due)

    async def _refresh(self, branch: TrackedBranch) -> tuple[TrackedBranch, Fetched]:
        async with self._slots:
            try:
                snapshot: PrSnapshot | None = await self._fetch(
                    branch.branch, branch.path
                )
            except Exception:  # noqa: BLE001
                # This is a background poll with nobody to propagate to: one
                # branch's failure (gh missing, bad output) is shown on its
                # row and retried, and must not stop the others.
                snapshot = None
        previous = self._cache.get(branch.key)
        fetched = Fetched(
            status=snapshot.status if snapshot else None,
            at=self._clock(),
            facts=snapshot.facts if snapshot else None,
            conflict=snapshot.conflict if snapshot else None,
            wall_at=self._wall_clock(),
        )
        self._cache[branch.key] = fetched
        if (
            previous is None
            or previous.status != fetched.status
            or previous.facts != fetched.facts
            or previous.conflict != fetched.conflict
        ):
            self._on_change(branch, fetched)
        return branch, fetched

    async def _pause(self) -> None:
        """Sleep one tick, or less when a browser connects meanwhile."""
        arrival = asyncio.ensure_future(self._hub.wait_for_arrival())
        nap = asyncio.ensure_future(self._sleep(self._tick))
        _, pending = await asyncio.wait(
            {arrival, nap}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

    async def run(self) -> None:
        while True:
            await self._hub.wait_for_subscriber()
            while self._hub.watched():
                # Cleared before the pass: a browser arriving during it makes
                # the pause below end at once, for a pass of its own.
                self._hub.clear_arrival()
                await self.run_once()
                await self._pause()


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
