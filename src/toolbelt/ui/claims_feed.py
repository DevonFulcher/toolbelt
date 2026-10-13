"""Keeps each branch's claim holders current for the connected pages.

Like ``StatusRefresher`` and ``TaskFeed`` it only works while a browser is
watching (it parks on the ``EventHub``; the same grace period applies). Every
tick (about a second) it re-reads the claims directory (cheap: a listing and
a few small JSON files) at the current time, so a holder whose ttl ran out
drops away on its own, and calls ``on_change`` with the branches whose holders
changed. The app turns that into one ``claims`` event that re-derives those
branches' next action and their stacked children's.

Resolving claims to branches asks git (``git worktree list`` per repo), so
that is done only when the *identity* of the live claims changed (a holder
appeared, left or claimed something else), or ``resolve_ttl`` has passed (a
branch may have been added to the stack store). Heartbeats, which only move
timestamps, never cause it.

The writer overwrites its file in place (not atomically), so one unreadable
read of a file that was fine a moment ago keeps its previous contents; a
second consecutive bad read drops it.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from toolbelt.task_claims.operations import collect_garbage
from toolbelt.task_claims.reading import Holder, Reading, read_claims, reclassify
from toolbelt.task_claims.resolution import TrackedTask, resolve_claim
from toolbelt.ui.live_status import EventHub

CLAIMS_TICK_SECONDS = 1.0
DEFAULT_RESOLVE_TTL_SECONDS = 15.0
#: An expired file is deleted once it has been expired this long (so a
#: heartbeat racing the deletion cannot lose a claim).
EXPIRED_FILE_GRACE = timedelta(seconds=10)

type BranchKey = tuple[int, str]


@dataclass(frozen=True)
class BranchHolder:
    """A live holder as it concerns one branch."""

    holder: str
    kind: str
    state: str
    link: str | None
    started_at: datetime
    #: Working, but not heard from for a while (probably hung).
    stale: bool


class ClaimsFeed:
    def __init__(
        self,
        *,
        directory: Callable[[], Path],
        tasks: Callable[[], list[TrackedTask]],
        hub: EventHub,
        on_change: Callable[[set[BranchKey]], None],
        wall_clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = CLAIMS_TICK_SECONDS,
        resolve_ttl: float = DEFAULT_RESOLVE_TTL_SECONDS,
    ) -> None:
        self._directory = directory
        self._tasks = tasks
        self._hub = hub
        self._on_change = on_change
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._tick = tick
        self._resolve_ttl = timedelta(seconds=resolve_ttl)
        self._view: dict[BranchKey, tuple[BranchHolder, ...]] = {}
        #: Holder files by name as last read well, for the one-bad-read grace.
        self._last_good: dict[str, Holder] = {}
        self._bad_reads: set[str] = set()
        self._reported_unreadable: frozenset[str] = frozenset()
        self._unreadable_last: frozenset[str] = frozenset()
        self._resolved_for: tuple | None = None
        self._resolved_at: datetime | None = None
        self._resolved: list[TrackedTask] = []

    # -- what the snapshot and the status derivation read -----------------------------

    def holders(self, repo_id: int, branch: str) -> tuple[BranchHolder, ...]:
        return self._view.get((repo_id, branch), ())

    def working(self, repo_id: int, branch: str) -> bool:
        """A live ``working`` holder claims the branch: it reads "With an agent"."""
        return any(h.state == "working" for h in self.holders(repo_id, branch))

    @property
    def unreadable(self) -> frozenset[str]:
        """Files that were unreadable on two consecutive polls (one bad read
        is a write in progress and is not reported)."""
        return self._reported_unreadable

    # -- polling ----------------------------------------------------------------------------

    def _holders_now(self, reading: Reading, now: datetime) -> list[Holder]:
        current = {h.holder: h for h in reading.holders}
        unreadable = {
            s.name.removesuffix(".json") for s in reading.skipped if "/" not in s.name
        }
        # A file that was fine and is unreadable once keeps its last contents.
        kept = {
            name: self._last_good[name]
            for name in unreadable
            if name in self._last_good
            and name not in self._bad_reads
            and name not in current
        }
        self._bad_reads = set(kept)
        self._reported_unreadable = frozenset(unreadable & self._unreadable_last)
        self._unreadable_last = frozenset(unreadable)
        self._last_good = current | kept
        return [reclassify(h, now) for h in self._last_good.values()]

    def poll(self) -> set[BranchKey]:
        """Re-reads the directory; returns (and reports through ``on_change``)
        the branches whose holders changed."""
        now = self._wall_clock()
        reading = read_claims(self._directory(), now)
        collect_garbage(self._directory(), reading, now, EXPIRED_FILE_GRACE)
        holders = [h for h in self._holders_now(reading, now) if h.live]
        signature = tuple(
            sorted(
                (
                    h.holder,
                    tuple(
                        sorted(
                            (c.kind, c.path if c.kind == "worktree" else c.ref)
                            for c in h.claims
                        )
                    ),
                )
                for h in holders
            )
        )
        if holders and (
            signature != self._resolved_for
            or self._resolved_at is None
            or now - self._resolved_at >= self._resolve_ttl
        ):
            self._resolved = self._tasks()
            self._resolved_for = signature
            self._resolved_at = now
        if not holders:
            self._resolved_for = None
        new: dict[BranchKey, dict[str, BranchHolder]] = {}
        for holder in holders:
            for claim in holder.claims:
                for task in resolve_claim(claim, self._resolved):
                    new.setdefault(task.key, {})[holder.holder] = BranchHolder(
                        holder=holder.holder,
                        kind=holder.file.kind,
                        state=holder.file.state,
                        link=holder.file.link,
                        started_at=holder.file.started_at,
                        stale=holder.stale,
                    )
        view = {
            key: tuple(sorted(by_holder.values(), key=lambda h: h.holder))
            for key, by_holder in new.items()
        }
        changed = {
            key
            for key in view.keys() | self._view.keys()
            if view.get(key) != self._view.get(key)
        }
        self._view = view
        if changed:
            self._on_change(changed)
        return changed

    async def run(self) -> None:
        while True:
            await self._hub.wait_for_subscriber()
            while self._hub.watched():
                self.poll()
                await self._sleep(self._tick)
