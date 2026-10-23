"""PRs queued to be merged as soon as their CI passes.

A PR that is approved and has nothing left but CI running can be queued from
its card. Once its CI has passed, so that its next action is Merge, the queue
merges it exactly as the Merge button does (``toolbelt git merge``).

Nothing is merged that is not what was queued: a queued PR is dropped, with a
note on its card saying why, when its head changed (new commits), when it was
closed, or when it stops being only a CI wait away from merging (CI failed, a
comment or a conflict appeared, the approval went away, its parent has not
merged). Queued PRs are kept in the stack DB, so a restart does not lose them.

Whether a PR is ready is read from the PR lookups the page already keeps
current; the watcher compares them every ``tick`` seconds and when a PR is
queued.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from toolbelt.git.stack.status import PrState
from toolbelt.git.stack.store import MergeQueueRow
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import Fetched, TrackedBranch

LOGGER = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 10.0

#: Longest part of a failed merge's output kept on the card (the end of it).
NOTICE_LIMIT = 600

#: Why a queued PR stopped being one CI wait away from merging, by the next
#: action it has instead.
_DROPPED_BECAUSE = {
    "fix_ci": "its CI failed",
    "resolve_conflicts": "it has conflicts",
    "address_review": "changes were requested",
    "resolve_comments": "it has unresolved comments",
    "blocked": "it is blocked on a PR it is stacked on",
    "wait_review": "its approval was removed",
    "request_review": "its approval was removed",
    "publish": "it is a draft again",
}


class MergeQueueRefused(Exception):
    """The PR cannot be queued."""


@dataclass(frozen=True)
class LandResult:
    ok: bool
    output: str


BranchKey = tuple[int, str]


class MergeQueue:
    def __init__(
        self,
        *,
        entries: Callable[[], list[MergeQueueRow]],
        add: Callable[[MergeQueueRow], None],
        remove: Callable[[int, str], None],
        branches: Callable[[], list[TrackedBranch]],
        fetched: Callable[[int, str], Fetched | None],
        next_kind: Callable[[int, str], str | None],
        would_merge: Callable[[int, str], bool],
        land: Callable[[int, str], Awaitable[LandResult]],
        wall_clock: Callable[[], datetime],
        changed: Callable[[set[BranchKey]], None],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        """``next_kind`` is what the card's Next says; ``would_merge`` whether a
        PR whose CI is running would have Merge as its next action once the CI
        passed; ``land(repo_id, branch)`` merges (the Merge button's work);
        ``changed`` is told which branches' queue standing changed."""
        self._entries = entries
        self._add = add
        self._remove = remove
        self._branches = branches
        self._fetched = fetched
        self._next_kind = next_kind
        self._would_merge = would_merge
        self._land = land
        self._wall_clock = wall_clock
        self._changed = changed
        self._sleep = sleep
        self._tick = tick
        self._queued: dict[BranchKey, MergeQueueRow] = {}
        self._merging: set[BranchKey] = set()
        # Why a PR left the queue, until it is queued again.
        self._notice: dict[BranchKey, str] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self.reload()

    def reload(self) -> None:
        self._queued = {(row.repo_id, row.branch): row for row in self._entries()}

    # ---- what the cards show ---------------------------------------------------

    def info(self, repo_id: int, branch: str) -> api.MergeQueueInfo | None:
        """``None`` when there is nothing to say: not queued, nothing that can
        be queued, no note."""
        key = (repo_id, branch)
        row = self._queued.get(key)
        notice = self._notice.get(key)
        can_queue = row is None and self._would_merge(repo_id, branch)
        merging = key in self._merging
        if row is None and not can_queue and notice is None and not merging:
            return None
        return api.MergeQueueInfo(
            can_queue=can_queue,
            queued=row is not None,
            merging=merging,
            queued_at=row.queued_at.isoformat() if row else None,
            notice=notice,
        )

    # ---- queueing --------------------------------------------------------------

    def queue(self, repo_id: int, branch: str) -> api.MergeQueueInfo:
        """Queues the branch's PR. Raises ``MergeQueueRefused`` when it is not
        approved and waiting only on CI."""
        key = (repo_id, branch)
        looked_up = self._fetched(repo_id, branch)
        if looked_up is None or looked_up.facts is None:
            raise MergeQueueRefused("The PR has not been looked up yet.")
        if key not in self._queued:
            if not self._would_merge(repo_id, branch):
                raise MergeQueueRefused(
                    "Only a PR that is approved and waiting on nothing but CI can "
                    "be queued."
                )
            row = MergeQueueRow(
                repo_id=repo_id,
                branch=branch,
                pr_number=looked_up.facts.number,
                head_oid=looked_up.facts.head_oid,
                queued_at=self._wall_clock(),
            )
            self._add(row)
            self._queued[key] = row
        self._notice.pop(key, None)
        self._changed({key})
        info = self.info(repo_id, branch)
        assert info is not None
        return info

    def merge_now(self, repo_id: int, branch: str) -> None:
        """Merges the branch's PR now, in the background (the Merge button): it
        reads ``merging`` until done, and a failure is its ``notice``. Raises
        ``MergeQueueRefused`` unless Merge is its next action."""
        key = (repo_id, branch)
        if key in self._merging:
            return
        if self._next_kind(repo_id, branch) != "merge":
            raise MergeQueueRefused(
                "Nothing to merge: the branch's next action is not Merge."
            )
        self._notice.pop(key, None)
        self._start_merge(key, queued=False)

    def _start_merge(self, key: BranchKey, *, queued: bool) -> None:
        task = asyncio.create_task(self._merge(key, queued=queued))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # Marked now, not once the task gets its turn, so nothing else starts it.
        self._merging.add(key)
        self._changed({key})

    def cancel(self, repo_id: int, branch: str) -> None:
        key = (repo_id, branch)
        if key in self._queued and key not in self._merging:
            self._drop(key, None)

    # ---- merging when CI has passed -----------------------------------------------

    def _drop(self, key: BranchKey, notice: str | None) -> None:
        self._queued.pop(key, None)
        self._remove(*key)
        if notice is None:
            self._notice.pop(key, None)
        else:
            self._notice[key] = notice
        self._changed({key})

    def check(self) -> None:
        """Compares every queued PR with what is known of it now: drops the
        ones that must not be merged, and starts merging the ones whose CI has
        passed. Starting a merge needs a running event loop."""
        tracked = {(b.repo_id, b.branch) for b in self._branches()}
        for key, row in list(self._queued.items()):
            if key in self._merging:
                continue
            if key not in tracked:
                self._drop(key, None)
                continue
            looked_up = self._fetched(*key)
            if looked_up is None or looked_up.facts is None or looked_up.status is None:
                continue  # not looked up yet: nothing to judge by
            if looked_up.status.pr in (PrState.MERGED, PrState.CLOSED):
                closed = looked_up.status.pr is PrState.CLOSED
                self._drop(
                    key, "The PR was closed without merging." if closed else None
                )
                continue
            if looked_up.facts.head_oid != row.head_oid:
                self._drop(
                    key,
                    "New commits were pushed after it was queued, so it was not "
                    "merged: queue it again when it is ready.",
                )
                continue
            kind = self._next_kind(*key)
            if kind == "merge":
                self._start_merge(key, queued=True)
            elif kind in ("wait_ci", "claimed", "autonomous", "deciding"):
                continue  # still waiting (a claimed branch is left to its agent)
            else:
                why = _DROPPED_BECAUSE.get(kind or "", f"its next action became {kind}")
                self._drop(key, f"Taken off the merge queue: {why}.")

    async def _merge(self, key: BranchKey, *, queued: bool) -> None:
        # One merge at a time: each one restacks the stack it belonged to.
        async with self._lock:
            if queued and key not in self._queued:  # cancelled while waiting
                self._merging.discard(key)
                self._changed({key})
                return
            notice = None
            what = "queued merge" if queued else "merge"
            try:
                result = await self._land(*key)
                if not result.ok:
                    notice = f"The {what} failed: {result.output[-NOTICE_LIMIT:]}"
            except Exception as error:  # a failed merge says why, and leaves the queue
                LOGGER.exception("%s failed", what)
                notice = f"The {what} failed: {error}"
            finally:
                self._merging.discard(key)
                self._drop(key, notice)

    async def idle(self) -> None:
        """Returns when no merge is in flight."""
        while self._tasks:
            await asyncio.gather(*self._tasks)

    async def run(self) -> None:
        """Checks the queue every ``tick`` seconds."""
        while True:
            self.check()
            await self._sleep(self._tick)


class MergeQueueLookup(Protocol):
    """What the snapshot reads of the queue."""

    def info(self, repo_id: int, branch: str) -> api.MergeQueueInfo | None: ...


class _NoMergeQueue:
    def info(self, repo_id: int, branch: str) -> api.MergeQueueInfo | None:
        return None


#: Stands in where nothing can be queued (as in tests that build a snapshot).
NO_MERGE_QUEUE: MergeQueueLookup = _NoMergeQueue()
