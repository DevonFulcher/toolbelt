"""PRs queued to be merged as soon as they are ready.

A PR whose next action is Waiting on CI, or Blocked on its parent PR, can be
queued from its card. Once its next action is Merge (CI passed, approved, no
comments or conflicts, its parent merged) the queue merges it exactly as the
Merge button does (``toolbelt git merge``), pinned to the head that was queued.

Every queued PR is merged only into the trunk (a PR queued on the trunk whose
base is another branch is dropped), and a stacked PR merges after its parents. Once its
parent has merged, the sync that ``toolbelt git merge`` runs pushes a merge
commit onto it; that one head change is accepted (the queue then pins the new
head), and the PR is merged only when its base is the trunk and its head
contains the parent's squash commit, so it is never merged into the parent's
old branch or without what the parent landed.

Nothing else is merged that is not what was queued: a queued PR is dropped, with
a note on its card saying why, when its head changed in any other way (new
commits), when it was closed, when its parent was closed without merging, or
when it needs something done (CI failed, a comment or a conflict appeared,
changes were requested). Waiting for CI, review or a parent keeps it queued.
Queued PRs are kept in the stack DB, so a restart does not lose them.

Whether a PR is ready is read from the PR lookups the page already keeps
current; the watcher compares them every ``tick`` seconds and when a PR is
queued.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from toolbelt.git.stack.status import PrState
from toolbelt.git.stack.store import MergeQueueRow
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import Fetched, TrackedBranch
from toolbelt.ui.merge_queue_pause import paused_until

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
    "publish": "it is a draft again",
}

#: Next actions a PR can have right after it was published, besides the ones
#: ``queueable`` accepts: its Slack post (still going out) or a review is next.
_WAITS_AFTER_PUBLISH = ("post_slack", "bump_slack", "wait_review", "request_review")

#: Next actions of a queued PR that only need time (or someone else): it stays
#: queued, and merges once its next action is Merge.
_STILL_WAITING = (
    "wait_ci",
    "wait_review",
    "request_review",
    "post_slack",
    "bump_slack",
    "blocked",
    "claimed",
    "autonomous",
    "deciding",
)


class MergeQueueRefused(Exception):
    """The PR cannot be queued."""


@dataclass(frozen=True)
class StackedOn:
    """The branch a PR is stacked on, and that branch's PR number (``None``
    while it has not been looked up)."""

    branch: str
    pr_number: int | None


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
        queueable: Callable[[int, str], bool],
        stacked_on: Callable[[int, str], StackedOn | None],
        parent_state: Callable[[MergeQueueRow], PrState | None],
        ready_to_land: Callable[[MergeQueueRow], bool],
        base_problem: Callable[[MergeQueueRow], str | None],
        is_sync_merge: Callable[[MergeQueueRow, str], bool],
        agents_enabled: Callable[[], bool] = lambda: False,
        agent_pushed: Callable[[MergeQueueRow, str], bool] = lambda row, head: False,
        land: Callable[[int, str, str | None], Awaitable[LandResult]],
        wall_clock: Callable[[], datetime],
        local_clock: Callable[[], datetime],
        changed: Callable[[set[BranchKey]], None],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        """``next_kind`` is what the card's Next says; ``queueable`` whether the
        PR's next action is one that can be queued (Waiting on CI, or Blocked on
        its parent); ``stacked_on`` what a branch is stacked on now (``None``
        for one on the trunk); ``parent_state`` the PR state of the parent a
        queued PR was stacked on (``None`` for one queued on the trunk);
        ``ready_to_land(row)`` whether a queued PR whose next action is Merge is
        based on the trunk and, when it had a parent, contains what its parent
        landed; ``base_problem(row)`` why a PR queued on the trunk is not based
        on it (``None`` when it is); ``is_sync_merge(row, new_head)`` whether ``new_head`` is
        what a sync adds to the queued head; ``agents_enabled()`` whether the
        autonomous agents are on (they resolve bot comments, so a queued PR
        whose only unresolved threads are bots' waits for them);
        ``agent_pushed(row, new_head)`` whether ``new_head`` is what an agent
        run started on the queued head pushed;
        ``local_clock`` is the time in the zone the pause hours mean (see
        ``merge_queue_pause``); ``land(repo_id, branch, head)`` merges, only if the PR's head is
        ``head`` when given (the Merge button's work); ``changed`` is told
        which branches' queue standing changed."""
        self._entries = entries
        self._add = add
        self._remove = remove
        self._branches = branches
        self._fetched = fetched
        self._next_kind = next_kind
        self._queueable = queueable
        self._stacked_on = stacked_on
        self._parent_state = parent_state
        self._ready_to_land = ready_to_land
        self._base_problem = base_problem
        self._is_sync_merge = is_sync_merge
        self._agents_enabled = agents_enabled
        self._agent_pushed = agent_pushed
        self._land = land
        self._wall_clock = wall_clock
        self._local_clock = local_clock
        self._was_paused = False
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
        can_queue = row is None and self._queueable(repo_id, branch)
        merging = key in self._merging
        if row is None and not can_queue and notice is None and not merging:
            return None
        return api.MergeQueueInfo(
            can_queue=can_queue,
            queued=row is not None,
            merging=merging,
            queued_at=row.queued_at.isoformat() if row else None,
            paused_until=self._paused_until_iso() if row else None,
            notice=notice,
        )

    def _paused_until_iso(self) -> str | None:
        resume = paused_until(self._local_clock())
        return resume.isoformat() if resume else None

    # ---- queueing --------------------------------------------------------------

    def queue(
        self, repo_id: int, branch: str, *, just_published: bool = False
    ) -> api.MergeQueueInfo:
        """Queues the branch's PR. Raises ``MergeQueueRefused`` unless its next
        action is Waiting on CI or Blocked on its parent PR. A PR that has just
        been published is also accepted while its Slack post or a review is next
        (CI may not have started yet, or may already have passed): the watcher
        keeps it queued through those waits."""
        key = (repo_id, branch)
        looked_up = self._fetched(repo_id, branch)
        if looked_up is None or looked_up.facts is None:
            raise MergeQueueRefused("The PR has not been looked up yet.")
        if key not in self._queued:
            waiting_after_publish = (
                just_published
                and self._next_kind(repo_id, branch) in _WAITS_AFTER_PUBLISH
            )
            if not (self._queueable(repo_id, branch) or waiting_after_publish):
                raise MergeQueueRefused(
                    "Only a PR that is waiting on CI, or approved and blocked on "
                    "its parent PR, can be queued."
                )
            parent = self._stacked_on(repo_id, branch)
            if parent is not None and parent.pr_number is None:
                raise MergeQueueRefused(
                    "The PR it is stacked on has not been looked up yet."
                )
            row = MergeQueueRow(
                repo_id=repo_id,
                branch=branch,
                pr_number=looked_up.facts.number,
                head_oid=looked_up.facts.head_oid,
                queued_at=self._wall_clock(),
                parent_branch=parent.branch if parent else None,
                parent_pr=parent.pr_number if parent else None,
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
        paused = paused_until(self._local_clock()) is not None
        if paused != self._was_paused:
            # The cards say when the queue resumes: tell them it started or ended.
            self._was_paused = paused
            self._changed(set(self._queued))
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
                if not (
                    self._is_sync_merge(row, looked_up.facts.head_oid)
                    or self._agent_pushed(row, looked_up.facts.head_oid)
                ):
                    self._drop(
                        key,
                        "New commits were pushed after it was queued, so it was not "
                        "merged: queue it again when it is ready.",
                    )
                    continue
                # The sync of a merged parent (or of a parent that took one), or
                # an agent's fix of its comments: the same PR, now carrying what
                # that brought. It is merged only as this head.
                row = dataclasses.replace(row, head_oid=looked_up.facts.head_oid)
                self._add(row)
                self._queued[key] = row
            kind = self._next_kind(*key)
            if self._parent_state(row) is PrState.CLOSED:
                self._drop(
                    key,
                    "Taken off the merge queue: the PR it is stacked on was closed "
                    "without merging.",
                )
            elif kind == "merge":
                problem = self._base_problem(row)
                if problem is not None:
                    self._drop(key, f"Taken off the merge queue: {problem}")
                elif not paused and self._ready_to_land(row):
                    self._start_merge(key, queued=True)
            elif kind in _STILL_WAITING:
                continue  # a claimed branch is left to its agent
            elif kind == "resolve_comments" and self._only_bot_comments(looked_up):
                continue  # an autonomous agent resolves bot comments
            else:
                why = _DROPPED_BECAUSE.get(kind or "", f"its next action became {kind}")
                self._drop(key, f"Taken off the merge queue: {why}.")

    def _only_bot_comments(self, looked_up: Fetched) -> bool:
        status = looked_up.status
        return (
            self._agents_enabled()
            and status is not None
            and 0 < status.open_comments == status.bot_comments
        )

    def _ready_after_wait(self, key: BranchKey) -> bool:
        row = self._queued.get(key)
        looked_up = self._fetched(*key)
        return (
            row is not None
            and looked_up is not None
            and looked_up.facts is not None
            and looked_up.facts.head_oid == row.head_oid
            and self._next_kind(*key) == "merge"
            and paused_until(self._local_clock()) is None
            and self._ready_to_land(row)
        )

    async def _merge(self, key: BranchKey, *, queued: bool) -> None:
        # One merge at a time: each one restacks the stack it belonged to.
        async with self._lock:
            # Cancelled while waiting for the lock, or no longer ready (the merge
            # ahead of it restacked it): it stays queued for the next check.
            if queued and not self._ready_after_wait(key):
                self._merging.discard(key)
                self._changed({key})
                return
            notice = None
            what = "queued merge" if queued else "merge"
            try:
                result = await self._land(
                    *key, self._queued[key].head_oid if queued else None
                )
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
