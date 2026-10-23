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
  due at the same moment) is spread over several rounds, not one burst;
* with a ``gate``, a due branch whose PR GitHub says is unchanged (a free
  conditional request) reuses its last lookup; the full lookup still runs once
  ``full_interval`` has passed;
* a repo whose GitHub webhooks are being forwarded here (``pushed``) is polled
  only every ``pushed_interval``, as a safety net: ``hint`` refetches a branch
  at once when a webhook says it changed.

Time and sleeping are injected so tests drive a pass at a time without waiting.
"""

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from toolbelt.git.stack.status import BranchStatus, PrState
from toolbelt.logger import logger
from toolbelt.ui.conflict_commit import ConflictCommit
from toolbelt.ui.github_requests import RateLimited
from toolbelt.ui.lookup_cache import SavedLookup
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.pr_fetch import PrSnapshot

# (branch, repo path) -> that branch's PR status and history.
StatusFetcher = Callable[[str, Path], Awaitable[PrSnapshot]]

DEFAULT_INTERVAL_SECONDS = 20.0
DEFAULT_SETTLED_INTERVAL_SECONDS = 300.0
DEFAULT_NO_PR_INTERVAL_SECONDS = 60.0
DEFAULT_FULL_INTERVAL_SECONDS = 600.0
#: A PR with unresolved review threads gets the full lookup this often: the
#: ETag gate cannot see a thread being resolved, and the count on the card
#: should follow it.
DEFAULT_COMMENTS_FULL_INTERVAL_SECONDS = 120.0
DEFAULT_PUSHED_INTERVAL_SECONDS = 300.0
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
    #: When (``clock`` time) the PR was last looked up in full. ``at`` is also
    #: moved by a poll that found nothing changed and reused the lookup.
    full_at: float = 0.0
    #: Why the latest attempt failed, when it did. ``status`` and the rest are
    #: then the last good lookup (``None`` when there never was one) and
    #: ``wall_at`` is when that was.
    error: str | None = None


class LookupPersistence(Protocol):
    """Where each branch's last good lookup is kept (see ``lookup_cache.py``).
    Called from worker threads."""

    def load(self) -> dict[tuple[int, str], SavedLookup]: ...

    def save(self, repo_id: int, branch: str, saved: SavedLookup) -> None: ...

    def delete(self, repo_id: int, branch: str) -> None: ...


class ChangeGate(Protocol):
    """Answers, cheaply, whether a branch's PR changed since its last lookup
    (see ``pr_gate.py``)."""

    async def unchanged(self, branch: TrackedBranch, previous: Fetched) -> bool: ...

    async def remember(self, branch: TrackedBranch, fetched: Fetched) -> None: ...

    async def export(
        self, branch: TrackedBranch, fetched: Fetched
    ) -> dict[str, str]: ...

    def restore(self, etags: dict[str, str]) -> None: ...


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
        gate: ChangeGate | None = None,
        full_interval: float = DEFAULT_FULL_INTERVAL_SECONDS,
        comments_full_interval: float = DEFAULT_COMMENTS_FULL_INTERVAL_SECONDS,
        pushed: Callable[[int], bool] = lambda repo_id: False,
        pushed_interval: float = DEFAULT_PUSHED_INTERVAL_SECONDS,
        no_pr_interval: float = DEFAULT_NO_PR_INTERVAL_SECONDS,
        persist: LookupPersistence | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._persist = persist
        self._jitter = jitter
        self._no_pr_interval = no_pr_interval
        self._gate = gate
        self._full_interval = full_interval
        self._comments_full_interval = comments_full_interval
        self._pushed = pushed
        self._pushed_interval = pushed_interval
        #: Branches a webhook said changed: refetched at once, gate skipped.
        self._hinted: set[tuple[int, str]] = set()
        self._hinted_wake = asyncio.Event()
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

    def _interval_for(self, key: tuple[int, str], fetched: Fetched) -> float:
        if fetched.status is not None and fetched.status.pr in _SETTLED_PR_STATES:
            return self._settled_interval
        if self._pushed(key[0]):
            return self._pushed_interval
        if fetched.status is not None and fetched.status.pr is PrState.NONE:
            return self._no_pr_interval
        return self._interval

    def _is_due(self, key: tuple[int, str], now: float) -> bool:
        fetched = self._cache.get(key)
        return (
            fetched is None
            or key in self._hinted
            or fetched.at + self._interval_for(key, fetched) <= now
        )

    def hint(self, keys: set[tuple[int, str]]) -> None:
        """Refetch these branches at once (a webhook said they changed)."""
        self._hinted |= keys
        self._hinted_wake.set()

    def refresh_all(self) -> int:
        """Looks every tracked branch up again at once, gate skipped (the
        Refresh button). Returns how many."""
        keys = {branch.key for branch in self._branches()}
        self.hint(keys)
        return len(keys)

    async def snapshot(self, branch: TrackedBranch, max_age: float) -> PrSnapshot:
        """The branch's PR as last looked up, for a caller that needs it even
        when no browser is watching (the autonomous agents). A lookup younger
        than ``max_age`` seconds is reused; an older one is refreshed through
        the same gate, saved lookups and rate-limit handling as the polling.
        Raises when the latest lookup failed: the caller must not act on a
        status that may be out of date."""
        cached = self._cache.get(branch.key)
        if (
            cached is None
            or cached.status is None
            or cached.error is not None
            or self._clock() - cached.at >= max_age
        ):
            _, cached = await self._refresh(branch)
        if cached.status is None or cached.error is not None:
            raise RuntimeError(cached.error or "no lookup of the PR")
        return PrSnapshot(cached.status, cached.facts, cached.conflict)

    async def restore(self) -> None:
        """Loads the saved lookups into the cache, with their real ages. A
        lookup whose full refresh is overdue gets a random point inside the
        interval, so the full lookups after a restart spread out instead of
        all running at once."""
        if self._persist is None:
            return
        saved = await asyncio.to_thread(self._persist.load)
        now, wall = self._clock(), self._wall_clock()
        for key, lookup in saved.items():
            age = max((wall - lookup.fetched_at).total_seconds(), 0.0)
            full_age = max((wall - lookup.full_at).total_seconds(), 0.0)
            if full_age >= self._full_interval:
                full_age = self._jitter() * self._full_interval
            self._cache[key] = Fetched(
                status=lookup.status,
                at=now - age,
                facts=lookup.facts,
                conflict=lookup.conflict,
                wall_at=lookup.fetched_at,
                full_at=now - full_age,
            )
            if self._gate is not None:
                self._gate.restore(lookup.etags)

    async def run_once(self) -> int:
        """Fetch every branch that is due. Returns how many were fetched."""
        branches = self._branches()
        keys = {branch.key for branch in branches}
        for key in set(self._cache) - keys:
            del self._cache[key]
            if self._persist is not None:
                await asyncio.to_thread(self._persist.delete, *key)
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
                        "fetched_at": (
                            _iso(result.wall_at) if result.wall_at else None
                        ),
                        "error": result.error,
                    }
                    for branch, result in fetched
                    if result.wall_at is not None or result.error is not None
                ]
            },
        )
        return len(due)

    async def _refresh(self, branch: TrackedBranch) -> tuple[TrackedBranch, Fetched]:
        previous = self._cache.get(branch.key)
        hinted = branch.key in self._hinted
        self._hinted.discard(branch.key)
        async with self._slots:
            if (
                previous is not None
                and not hinted
                and await self._unchanged(branch, previous)
            ):
                reused = replace(previous, at=self._clock(), wall_at=self._wall_clock())
                self._cache[branch.key] = reused
                return branch, reused
            try:
                snapshot = await self._fetch(branch.branch, branch.path)
                error = None
            except Exception as failure:  # noqa: BLE001
                # This is a background poll with nobody to propagate to: one
                # branch's failure (GitHub refusing, bad output) is shown on
                # its card and retried, and must not stop the others.
                snapshot = None
                error = (
                    str(failure)
                    if isinstance(failure, RateLimited)
                    else f"{type(failure).__name__}: {failure}"
                )
            now = self._clock()
            if snapshot is None:
                # The last good lookup stays, with its own age and the error.
                fetched = (
                    replace(previous, at=now, error=error)
                    if previous is not None and previous.status is not None
                    else Fetched(status=None, at=now, error=error)
                )
            else:
                fetched = Fetched(
                    status=snapshot.status,
                    at=now,
                    facts=snapshot.facts,
                    conflict=snapshot.conflict,
                    wall_at=self._wall_clock(),
                    full_at=now,
                )
                if self._gate is not None:
                    # Without ETags the next poll simply does the full lookup.
                    with contextlib.suppress(Exception):
                        await self._gate.remember(branch, fetched)
                await self._save(branch, fetched)
        self._cache[branch.key] = fetched
        if (
            previous is None
            or previous.status != fetched.status
            or previous.facts != fetched.facts
            or previous.conflict != fetched.conflict
        ):
            self._on_change(branch, fetched)
        return branch, fetched

    async def _save(self, branch: TrackedBranch, fetched: Fetched) -> None:
        if self._persist is None or fetched.status is None or fetched.wall_at is None:
            return
        try:
            etags = (
                await self._gate.export(branch, fetched)
                if self._gate is not None
                else {}
            )
            saved = SavedLookup(
                status=fetched.status,
                facts=fetched.facts,
                conflict=fetched.conflict,
                fetched_at=fetched.wall_at,
                full_at=fetched.wall_at,
                etags=etags,
            )
            await asyncio.to_thread(self._persist.save, *branch.key, saved)
        except Exception:  # noqa: BLE001
            # Saving only helps the next start: never stop the poll for it.
            logger.exception("could not save the lookup of %s", branch.branch)

    async def _unchanged(self, branch: TrackedBranch, previous: Fetched) -> bool:
        if (
            self._gate is None
            or previous.status is None
            # The gate has seen newer ETags than a lookup that failed: only a
            # full lookup can bring the data up to them.
            or previous.error is not None
            or self._clock() - previous.full_at >= self._full_interval_of(previous)
        ):
            return False
        try:
            return await self._gate.unchanged(branch, previous)
        except Exception:  # noqa: BLE001
            # The gate only saves a lookup: when it cannot answer, do the lookup.
            return False

    def _full_interval_of(self, previous: Fetched) -> float:
        if previous.status is not None and previous.status.open_comments > 0:
            return min(self._comments_full_interval, self._full_interval)
        return self._full_interval

    async def _pause(self) -> None:
        """Sleep one tick, or less when a browser connects or a webhook hints
        meanwhile."""
        arrival = asyncio.ensure_future(self._hub.wait_for_arrival())
        hinted = asyncio.ensure_future(self._hinted_wake.wait())
        nap = asyncio.ensure_future(self._sleep(self._tick))
        _, pending = await asyncio.wait(
            {arrival, hinted, nap}, return_when=asyncio.FIRST_COMPLETED
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
                self._hinted_wake.clear()
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
        #: Called when it publishes ``structure``.
        self.on_change: Callable[[], None] | None = None

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
        if self.on_change is not None:
            self.on_change()
        return True

    async def run(self) -> None:
        while True:
            await self._hub.wait_for_subscriber()
            self.check()
            await self._sleep(self._tick)
