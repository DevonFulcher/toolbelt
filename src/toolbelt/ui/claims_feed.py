"""Keeps each branch's claim holders current, and records what was claimed.

Unlike ``StatusRefresher`` and ``TaskFeed`` it runs whenever ``tt ui`` does,
browser or not: reading the claims directory is local and cheap (a listing
and a few small JSON files), and the claim files are overwritten every turn,
so the record of who claimed what is only as complete as the reads. Every
tick (about a second) it re-reads the directory at the current time, so a
holder whose ttl ran out, or whose writer process (recorded in its file) died,
drops away on its own, and calls ``on_change`` with
the branches whose holders changed. The app turns that into one ``claims``
event that re-derives those branches' next action and their stacked
children's. Each live claim it reads is also passed to ``record`` (at most
every ``RECORD_INTERVAL``), which is how claims reach the ``claim_spans``
table.

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

from toolbelt.task_claims.claim_file import Claim, ClaimActivity
from toolbelt.task_claims.operations import collect_garbage
from toolbelt.task_claims.process_liveness import ProcessLiveness
from toolbelt.task_claims.reading import (
    SYSTEM_LIVENESS,
    Holder,
    Reading,
    read_claims,
    reclassify,
)
from toolbelt.task_claims.resolution import TrackedTask, resolve_claim

CLAIMS_TICK_SECONDS = 1.0
DEFAULT_RESOLVE_TTL_SECONDS = 15.0
#: An expired file is deleted once it has been expired this long (so a
#: heartbeat racing the deletion cannot lose a claim).
EXPIRED_FILE_GRACE = timedelta(seconds=10)
#: A claim already recorded is not recorded again for this long.
RECORD_INTERVAL = timedelta(seconds=5)
#: The longest a claim may go unrecorded and still be one span (it must
#: exceed ``RECORD_INTERVAL`` and ride out a stalled poll).
SPAN_MAX_GAP = timedelta(seconds=30)

type BranchKey = tuple[int, str]


@dataclass(frozen=True)
class ClaimObservation:
    """One live claim as read at ``seen_at``, for the claim record."""

    holder: str
    session_id: str | None
    agent_id: str | None
    holder_kind: str
    claim_kind: str
    #: The claimed worktree path or ref.
    target: str
    #: The tracked task it resolved to (None: it matched none).
    task_id: str | None
    touched_at: datetime
    seen_at: datetime
    #: The holder's state when it was read (``working``, ``idle``, ``blocked``).
    state: str | None = None


def _target(claim: Claim) -> str:
    return claim.path if claim.kind == "worktree" else claim.ref


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
    #: What it is running, when its file says.
    activity: ClaimActivity | None = None


class ClaimsFeed:
    def __init__(
        self,
        *,
        directory: Callable[[], Path],
        tasks: Callable[[], list[TrackedTask]],
        on_change: Callable[[set[BranchKey]], None],
        record: Callable[[ClaimObservation], None],
        wall_clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = CLAIMS_TICK_SECONDS,
        resolve_ttl: float = DEFAULT_RESOLVE_TTL_SECONDS,
        liveness: ProcessLiveness = SYSTEM_LIVENESS,
        deciding: Callable[[int, str], bool] = lambda repo_id, branch: False,
        validated: Callable[[int, str], bool] = lambda repo_id, branch: False,
    ) -> None:
        #: Whether the task has a question for the user that is not answered
        #: (decisions are not claim files; the feed only hands the answer on).
        self._deciding = deciding
        self._validated = validated
        #: Checks each tick that a holder's recorded process still runs; it
        #: caches start times, so a tick costs a syscall per holder.
        self._liveness = liveness
        self._directory = directory
        self._tasks = tasks
        self._on_change = on_change
        self._record = record
        #: When each live claim was last recorded.
        self._recorded_at: dict[tuple, datetime] = {}
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
        """A live ``working`` holder (not an autonomous agent) claims the
        branch: it reads "With an agent"."""
        return any(
            h.state == "working" and h.kind != "autonomous"
            for h in self.holders(repo_id, branch)
        )

    def autonomous(self, repo_id: int, branch: str) -> bool:
        """A live ``working`` autonomous agent holds the branch: it reads
        "Autonomous agent" (unless ``working``)."""
        return any(
            h.state == "working" and h.kind == "autonomous"
            for h in self.holders(repo_id, branch)
        )

    def validated(self, repo_id: int, branch: str) -> bool:
        """A validate run reviewed the branch and no changes were requested
        since (see ``personal_review``)."""
        return self._validated(repo_id, branch)

    def deciding(self, repo_id: int, branch: str) -> bool:
        """An agent asked the user something about the branch and it is not
        answered yet: it reads "Deciding"."""
        return self._deciding(repo_id, branch)

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
        return [reclassify(h, now, self._liveness) for h in self._last_good.values()]

    def poll(self) -> set[BranchKey]:
        """Re-reads the directory; returns (and reports through ``on_change``)
        the branches whose holders changed."""
        now = self._wall_clock()
        reading = read_claims(self._directory(), now, self._liveness)
        collect_garbage(
            self._directory(), reading, now, EXPIRED_FILE_GRACE, self._liveness
        )
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
        recorded: dict[tuple, datetime] = {}
        for holder in holders:
            for claim in holder.claims:
                matched = resolve_claim(claim, self._resolved)
                for task in matched:
                    new.setdefault(task.key, {})[holder.holder] = BranchHolder(
                        holder=holder.holder,
                        kind=holder.file.kind,
                        state=holder.file.state,
                        link=holder.file.link,
                        started_at=holder.file.started_at,
                        stale=holder.stale,
                        activity=holder.file.activity,
                    )
                task_ids: list[str | None] = [task.task_id for task in matched] or [
                    None
                ]
                for task_id in task_ids:
                    key = (
                        holder.holder,
                        claim.kind,
                        _target(claim),
                        task_id,
                        holder.file.state,
                    )
                    last = self._recorded_at.get(key)
                    if last is None or now - last >= RECORD_INTERVAL:
                        self._record(
                            ClaimObservation(
                                holder=holder.holder,
                                session_id=holder.file.session_id,
                                agent_id=holder.file.agent_id,
                                holder_kind=holder.file.kind,
                                claim_kind=claim.kind,
                                target=_target(claim),
                                task_id=task_id,
                                touched_at=claim.touched_at,
                                seen_at=now,
                                state=holder.file.state,
                            )
                        )
                        last = now
                    recorded[key] = last
        self._recorded_at = recorded
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
            self.poll()
            await self._sleep(self._tick)
