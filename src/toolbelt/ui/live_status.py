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
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
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
#: A merged or closed PR gets the full lookup this often. The ETag gate still
#: shows a new comment within one poll; only a thread being resolved or the
#: base branch moving waits for this lookup, and those rarely matter once a PR
#: is settled.
DEFAULT_SETTLED_FULL_INTERVAL_SECONDS = 3600.0
#: An open PR with unresolved review threads gets the full lookup this often:
#: the ETag gate cannot see a thread being resolved, and the count on the card
#: should follow it. A merged or closed PR uses the settled full interval.
DEFAULT_COMMENTS_FULL_INTERVAL_SECONDS = 120.0
DEFAULT_PUSHED_INTERVAL_SECONDS = 300.0
#: A webhook hint starts a lookup of a branch only this long after its last one
#: (see ``StatusRefresher.hint``). It is the poll interval, the pace a branch is
#: looked up at where no webhooks arrive.
DEFAULT_HINT_GAP_SECONDS = DEFAULT_INTERVAL_SECONDS
DEFAULT_CONCURRENCY = 4
DEFAULT_TICK_SECONDS = 2.0
DEFAULT_GRACE_SECONDS = 5.0

_SETTLED_PR_STATES = (PrState.MERGED, PrState.CLOSED)


class LookupReason(StrEnum):
    """Why a poll ran the full (GraphQL) lookup instead of reusing the last
    one; counted in ``StatusRefresher.lookups`` so what spends the GraphQL
    budget can be read off ``/api/github-requests``."""

    FIRST = "first"
    HINTED = "hinted"
    REFRESH = "refresh"
    NO_GATE = "no_gate"
    AFTER_FAILURE = "after_failure"
    MAX_AGE = "max_age"
    GATE_CHANGED = "gate_changed"
    GATE_FAILED = "gate_failed"


#: The reasons for a lookup that did not start from the gate's answer, so the
#: ETags the gate holds are older than the data the lookup reads.
_LOOKUPS_BEFORE_THE_GATE_ASKED = frozenset(
    {
        LookupReason.HINTED,
        LookupReason.REFRESH,
        LookupReason.MAX_AGE,
        LookupReason.AFTER_FAILURE,
    }
)


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
        settled_full_interval: float = DEFAULT_SETTLED_FULL_INTERVAL_SECONDS,
        pushed: Callable[[int], bool] = lambda repo_id: False,
        pushed_interval: float = DEFAULT_PUSHED_INTERVAL_SECONDS,
        hint_gap: float = DEFAULT_HINT_GAP_SECONDS,
        no_pr_interval: float = DEFAULT_NO_PR_INTERVAL_SECONDS,
        persist: LookupPersistence | None = None,
        jitter: Callable[[], float] = random.random,
        observe: Callable[[int, str, BranchStatus, datetime], bool] | None = None,
    ) -> None:
        self._persist = persist
        self._observe = observe
        self._jitter = jitter
        self._no_pr_interval = no_pr_interval
        self._gate = gate
        self._full_interval = full_interval
        self._comments_full_interval = comments_full_interval
        self._settled_full_interval = settled_full_interval
        self._pushed = pushed
        self._pushed_interval = pushed_interval
        self._hint_gap = hint_gap
        #: Branches a webhook said changed: refetched, gate skipped (once
        #: ``hint_gap`` has passed since their last lookup).
        self._hinted: set[tuple[int, str]] = set()
        #: Branches the user asked to refresh: the same, by the card's button.
        self._refreshing: set[tuple[int, str]] = set()
        #: How many full lookups ran, by why.
        self.lookups: Counter[LookupReason] = Counter()
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
            or key in self._refreshing
            or (key in self._hinted and self._hint_ready(fetched, now))
            or fetched.at + self._interval_for(key, fetched) <= now
        )

    def _hint_ready(self, previous: Fetched, now: float) -> bool:
        """Whether a webhook hint may start a lookup of the branch. A burst of
        webhooks (every job of a CI run sends some) must not become a burst of
        lookups: the branch is looked up at most once per ``hint_gap``. A hint
        that comes sooner waits for that and then counts once."""
        return now - previous.full_at >= self._hint_gap

    def hint(self, keys: set[tuple[int, str]]) -> None:
        """Refetch these branches soon (a webhook said they changed): at once,
        unless one was looked up less than ``hint_gap`` ago (see
        ``_hint_ready``)."""
        self._hinted |= keys
        self._hinted_wake.set()

    def refresh(self, repo_id: int, branch: str) -> bool:
        """Looks the branch up again at once, gate skipped (the card's Refresh
        button). ``False`` when the branch is not tracked."""
        if (repo_id, branch) not in {b.key for b in self._branches()}:
            return False
        self._refreshing.add((repo_id, branch))
        self._hinted_wake.set()
        return True

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
            interval = self._full_interval_for(lookup.status)
            if full_age >= interval:
                full_age = self._jitter() * interval
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
        hinted = branch.key in self._hinted and (
            previous is None or self._hint_ready(previous, self._clock())
        )
        refreshing = branch.key in self._refreshing
        self._refreshing.discard(branch.key)
        async with self._slots:
            reason = await self._reason_to_look_up(
                branch, previous, hinted=hinted, refreshing=refreshing
            )
            if reason is None:
                assert previous is not None
                reused = replace(previous, at=self._clock(), wall_at=self._wall_clock())
                self._cache[branch.key] = reused
                return branch, reused
            self.lookups[reason] += 1
            # Whatever the reason, this lookup answers a pending hint.
            self._hinted.discard(branch.key)
            if reason in _LOOKUPS_BEFORE_THE_GATE_ASKED:
                await self._catch_up_etags(branch, previous)
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
                await self._record_transitions(branch, fetched)
        self._cache[branch.key] = fetched
        if (
            previous is None
            or previous.status != fetched.status
            or previous.facts != fetched.facts
            or previous.conflict != fetched.conflict
        ):
            self._on_change(branch, fetched)
        return branch, fetched

    async def _record_transitions(
        self, branch: TrackedBranch, fetched: Fetched
    ) -> None:
        """Note a change of the branch's conflict or CI status for its timeline
        (``status_transitions.py``). The write moves the database's data
        version, which the watcher turns into a refresh of the card extras."""
        if self._observe is None or fetched.status is None or fetched.wall_at is None:
            return
        try:
            await asyncio.to_thread(
                self._observe,
                branch.repo_id,
                branch.branch,
                fetched.status,
                fetched.wall_at,
            )
        except Exception:  # noqa: BLE001
            # Only the timeline's history: never stop the poll for it.
            logger.exception("could not record the status of %s", branch.branch)

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

    async def _catch_up_etags(
        self, branch: TrackedBranch, previous: Fetched | None
    ) -> None:
        """Reads the gate's ETags for a lookup the gate did not ask for (see
        ``_LOOKUPS_BEFORE_THE_GATE_ASKED``). Left as they were, the next poll
        would find them changed and look the PR up a second time for the very
        change this lookup is about. They are read before the lookup, not after
        it, so a change that lands meanwhile is still seen."""
        if self._gate is None or previous is None:
            return
        with contextlib.suppress(Exception):
            await self._gate.unchanged(branch, previous)

    async def _reason_to_look_up(
        self,
        branch: TrackedBranch,
        previous: Fetched | None,
        *,
        hinted: bool,
        refreshing: bool,
    ) -> LookupReason | None:
        """Why the full lookup has to run, or ``None`` when the last one still
        holds: the gate says the PR did not change and the lookup is younger
        than its full interval."""
        if previous is None or previous.status is None:
            return LookupReason.FIRST
        if refreshing:
            return LookupReason.REFRESH
        if hinted:
            return LookupReason.HINTED
        if self._gate is None:
            return LookupReason.NO_GATE
        if previous.error is not None:
            # The gate has seen newer ETags than a lookup that failed: only a
            # full lookup can bring the data up to them.
            return LookupReason.AFTER_FAILURE
        if self._clock() - previous.full_at >= self._full_interval_for(previous.status):
            return LookupReason.MAX_AGE
        try:
            if await self._gate.unchanged(branch, previous):
                return None
        except Exception:  # noqa: BLE001
            # The gate only saves a lookup: when it cannot answer, do the lookup.
            return LookupReason.GATE_FAILED
        return LookupReason.GATE_CHANGED

    def _full_interval_for(self, status: BranchStatus | None) -> float:
        if status is not None and status.pr in _SETTLED_PR_STATES:
            return self._settled_full_interval
        if status is not None and status.open_comments > 0:
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
