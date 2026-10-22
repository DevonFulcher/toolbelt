"""Gives the snapshot and the live status each branch's Slack standing.

``SlackTracker`` keeps the posts toolbelt recorded (the ``slack_posts`` table)
in memory, keyed by PR, and answers per branch from them and the PR's last
lookup: whether it is posted, when it was last asked about, and whether a bump
is due (see ``slack_posts``). It never reads Slack. ``reload`` re-reads the
table, after a post is recorded.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

from toolbelt.git.stack.store import SlackPostRow
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import Fetched, TrackedBranch
from toolbelt.ui.next_action import SlackContext
from toolbelt.ui.slack_posts import SlackState, slack_state


class SlackLookup(Protocol):
    """Where a branch's PR stands in Slack (``SlackTracker`` is one)."""

    def context(
        self, repo_id: int, branch: str, fetched: Fetched | None, now: datetime
    ) -> SlackContext | None: ...

    def info(
        self, repo_id: int, branch: str, fetched: Fetched | None, now: datetime
    ) -> api.SlackInfo | None: ...


class _NoSlack:
    def context(
        self, repo_id: int, branch: str, fetched: Fetched | None, now: datetime
    ) -> SlackContext | None:
        return None

    def info(
        self, repo_id: int, branch: str, fetched: Fetched | None, now: datetime
    ) -> api.SlackInfo | None:
        return None


NO_SLACK: SlackLookup = _NoSlack()


class SlackTracker:
    def __init__(
        self,
        *,
        posts: Callable[[], list[SlackPostRow]],
        slug_of: Callable[[int], str | None],
        bump_after: timedelta,
        permalink: Callable[[str, str], str | None] = lambda channel, ts: None,
    ) -> None:
        """``slug_of`` gives a repo's ``owner/name`` (lower case);
        ``permalink(channel, ts)`` the link to a message (``None``: unknown)."""
        self._posts = posts
        self._slug_of = slug_of
        self._bump_after = bump_after
        self._permalink = permalink
        self._by_pr: dict[tuple[str, int], list[SlackPostRow]] = {}
        # What a background post is doing, kept in memory: the PRs a message is
        # being posted for, and what the last one that finished had to say
        # (why it failed, or what Slack's copy lacked).
        self._posting: set[tuple[str, int]] = set()
        self._notice: dict[tuple[str, int], str] = {}
        self.reload()

    def reload(self) -> set[tuple[str, int]]:
        """Reads the posts again; returns the PRs (``(owner/name, number)``)
        whose posts are not what they were."""
        by_pr: dict[tuple[str, int], list[SlackPostRow]] = {}
        for row in self._posts():
            by_pr.setdefault((row.repo, row.pr_number), []).append(row)
        before = self._by_pr
        self._by_pr = by_pr
        return {
            pr for pr in before.keys() | by_pr.keys() if before.get(pr) != by_pr.get(pr)
        }

    def is_posting(self, slug: str, number: int) -> bool:
        return (slug, number) in self._posting

    def begin_posting(self, prs: list[tuple[str, int]]) -> None:
        self._posting.update(prs)
        for pr in prs:
            self._notice.pop(pr, None)

    def end_posting(self, prs: list[tuple[str, int]], notice: str | None) -> None:
        self._posting.difference_update(prs)
        if notice:
            for pr in prs:
                self._notice[pr] = notice

    def state(
        self, repo_id: int, branch: str, fetched: Fetched | None, now: datetime
    ) -> SlackState | None:
        """``None`` while the branch has no PR (or its lookup has not come)."""
        if fetched is None or fetched.status is None or fetched.facts is None:
            return None
        slug = self._slug_of(repo_id)
        if slug is None:
            return None
        return slack_state(
            self._by_pr.get((slug, fetched.facts.number), []),
            facts=fetched.facts,
            status=fetched.status,
            now=now,
            bump_after=self._bump_after,
        )

    def context(
        self, repo_id: int, branch: str, fetched: Fetched | None, now: datetime
    ) -> SlackContext | None:
        state = self.state(repo_id, branch, fetched, now)
        if state is None:
            return None
        return SlackContext(posted=state.posted, bump_due=state.bump_due)

    def info(
        self, repo_id: int, branch: str, fetched: Fetched | None, now: datetime
    ) -> api.SlackInfo | None:
        state = self.state(repo_id, branch, fetched, now)
        if state is None:
            return None
        pr = (self._slug_of(repo_id) or "", fetched.facts.number)  # type: ignore[union-attr]
        rows = self._by_pr.get(pr, [])
        last = max(rows, key=lambda row: row.posted_at) if rows else None
        return api.SlackInfo(
            posted=state.posted,
            last_kind=state.last_kind,  # type: ignore[arg-type]
            last_at=state.last_at.isoformat() if state.last_at else None,
            bump_due=state.bump_due,
            posting=pr in self._posting,
            notice=self._notice.get(pr),
            thread_link=self._permalink(last.channel, last.thread_ts) if last else None,
            assignee=last.assignee_name if last else None,
        )


DEFAULT_BUMP_TICK_SECONDS = 60.0


class BumpWatcher:
    """Says when a bump falls due. That happens with time passing, not with
    any change on GitHub, so nothing else would announce it: every ``tick``
    seconds it compares each branch's bump-due flag with the last one it saw
    and calls ``on_change`` with the branches whose flag changed."""

    def __init__(
        self,
        *,
        tracker: SlackTracker,
        branches: Callable[[], list[TrackedBranch]],
        fetched: Callable[[int, str], Fetched | None],
        wall_clock: Callable[[], datetime],
        on_change: Callable[[set[tuple[int, str]]], None],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = DEFAULT_BUMP_TICK_SECONDS,
    ) -> None:
        self._tracker = tracker
        self._branches = branches
        self._fetched = fetched
        self._wall_clock = wall_clock
        self._on_change = on_change
        self._sleep = sleep
        self._tick = tick
        self._due: dict[tuple[int, str], bool] = {}

    def check(self) -> set[tuple[int, str]]:
        """The branches whose bump-due flag changed since the last check (the
        first check only records them)."""
        now = self._wall_clock()
        first = not self._due
        changed: set[tuple[int, str]] = set()
        for branch in self._branches():
            state = self._tracker.state(
                branch.repo_id, branch.branch, self._fetched(*branch.key), now
            )
            due = state.bump_due if state else False
            if self._due.get(branch.key, False) != due and not first:
                changed.add(branch.key)
            self._due[branch.key] = due
        return changed

    async def run(self) -> None:
        while True:
            await self._sleep(self._tick)
            changed = self.check()
            if changed:
                self._on_change(changed)
