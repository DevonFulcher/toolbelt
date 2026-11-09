"""Merged PRs of removed branches that received review comments the user has
not acknowledged: they stay on the page as cards (``snapshot.landed_branch``
builds a card the way it builds a tracked branch's, with no stack and no
worktree).

A branch is removed (``toolbelt git sync``) soon after its PR merges, which
takes its card off the page. A merged PR that received comments must not vanish
that way (see ``next_action``'s Acknowledge), so ``LandedPrFeed`` looks at the
branches removed as the cleanup after a merge: for each one whose PR the user
has not acknowledged it asks GitHub, by PR number, for the PR's comments (see
``pr_fetch.review_comments``). A PR with none is marked acknowledged at once, so
it never shows and is not looked up again; one with some is kept as a card, in
memory, until it is acknowledged. A failed lookup is retried at the next pass.

Only branches removed within ``WINDOW`` are considered, so a pass never scans or
fetches the whole history.

Refreshed like ``UntrackedPrFeed``: only while a browser is watching, publishing
a ``structure`` event (the page fetches the snapshot again) when the list
changed. The stack DB, GitHub and the clock
are injected so tests drive a pass at a time.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from toolbelt.git.stack.store import RemovedBranch
from toolbelt.ui.live_status import DEFAULT_TICK_SECONDS, EventHub
from toolbelt.ui.pr_activity import ReviewComment
from toolbelt.ui.pr_fetch import PrComments

DEFAULT_INTERVAL_SECONDS = 120.0

#: How far back a removed branch's merged PR is still looked at.
WINDOW = timedelta(days=30)

#: A removed branch that landed with a known PR, as ``(branch, repo name)``.
type LandedBranch = tuple[RemovedBranch, str]


@dataclass(frozen=True)
class LandedPr:
    """A removed branch's merged PR that has comments to acknowledge."""

    repo_id: int
    #: The removed branch's name.
    branch: str
    task: str | None
    task_tracker: str | None
    started_at: datetime | None
    removed_at: datetime
    pr_number: int
    pr_url: str
    title: str
    comments: tuple[ReviewComment, ...]


class LandedPrFeed:
    def __init__(
        self,
        *,
        landed: Callable[[datetime], list[LandedBranch]],
        acknowledged: Callable[[int], frozenset[int]],
        acknowledge: Callable[[int, int], None],
        comments: Callable[[int, int], Awaitable[PrComments]],
        hub: EventHub,
        wall_clock: Callable[[], datetime],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        tick: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._landed = landed
        self._acknowledged = acknowledged
        self._acknowledge = acknowledge
        self._comments = comments
        self._hub = hub
        self._wall_clock = wall_clock
        self._clock = clock
        self._sleep = sleep
        self._interval = interval
        self._tick = tick
        self._last_at: float | None = None
        #: The cards of the PRs found to have comments, by ``(repo, PR)``.
        self._cards: dict[tuple[int, int], LandedPr] = {}
        self._current: list[LandedPr] = []

    def get(self) -> list[LandedPr]:
        return self._current

    def is_due(self) -> bool:
        return self._last_at is None or self._clock() - self._last_at >= self._interval

    async def _look_up(self, branch: RemovedBranch, repo: str) -> None:
        assert branch.pr is not None
        key = (branch.repo_id, branch.pr.number)
        try:
            found = await self._comments(branch.repo_id, branch.pr.number)
        except Exception:  # noqa: BLE001
            # Nobody to propagate to: leave it unlooked-up, so the next pass
            # tries again.
            return
        if not found.comments:
            self._acknowledge(*key)
            return
        self._cards[key] = LandedPr(
            repo_id=branch.repo_id,
            branch=branch.name,
            task=branch.task,
            task_tracker=branch.task_tracker,
            started_at=branch.started_at,
            removed_at=branch.removed_at,
            pr_number=branch.pr.number,
            pr_url=found.url,
            title=found.title,
            comments=found.comments,
        )

    async def run_once(self) -> bool:
        """Refresh now. Returns whether the list changed (and was published)."""
        rows = await asyncio.to_thread(self._landed, self._wall_clock() - WINDOW)
        acknowledged = {
            repo_id: await asyncio.to_thread(self._acknowledged, repo_id)
            for repo_id in {branch.repo_id for branch, _ in rows}
        }
        pending = [
            (branch, repo)
            for branch, repo in rows
            if branch.pr is not None
            and branch.pr.number not in acknowledged[branch.repo_id]
        ]
        for branch, repo in pending:
            assert branch.pr is not None
            if (branch.repo_id, branch.pr.number) not in self._cards:
                await self._look_up(branch, repo)
        current = [
            self._cards[(branch.repo_id, branch.pr.number)]
            for branch, _ in pending
            if branch.pr is not None
            and (branch.repo_id, branch.pr.number) in self._cards
        ]
        self._last_at = self._clock()
        if current == self._current:
            return False
        self._current = current
        self._publish()
        return True

    def acknowledged(self, repo_id: int, pr_number: int) -> None:
        """The user acknowledged this PR (already stored): its card goes."""
        self._cards.pop((repo_id, pr_number), None)
        self._current = [
            card
            for card in self._current
            if (card.repo_id, card.pr_number) != (repo_id, pr_number)
        ]
        self._publish()

    def _publish(self) -> None:
        # The cards are part of the snapshot: the page fetches it again.
        self._hub.publish("structure", {})

    async def run(self) -> None:
        while True:
            await self._hub.wait_for_subscriber()
            while self._hub.watched():
                if self.is_due():
                    await self.run_once()
                await self._sleep(self._tick)
