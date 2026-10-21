"""Where each branch's PR stands in Slack, from the recorded posts: the
tracker the snapshot reads, and the watcher that announces a bump falling due."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import SlackPostRow
from toolbelt.ui.live_status import Fetched, TrackedBranch
from toolbelt.ui.next_action import SlackContext
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.slack_tracker import BumpWatcher, SlackTracker

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
STATUS = BranchStatus(
    pr=PrState.OPEN,
    ci=CiState.SUCCESS,
    review=ReviewState.REQUESTED,
    url="https://github.com/o/r/pull/5",
)
FACTS = PrFacts(
    number=5,
    title="Add it",
    author="devon",
    base_ref="main",
    head_oid="abc",
    state="OPEN",
    is_draft=False,
    created_at=NOW - timedelta(days=3),
    merged_at=None,
    closed_at=None,
    requested_reviewers=(),
    commit_rollups=(),
    items=(),
)
FETCHED = Fetched(status=STATUS, at=0.0, facts=FACTS)


def row(hours_ago: float, *, number: int = 5, kind: str = "post") -> SlackPostRow:
    return SlackPostRow(
        channel="C1",
        ts=f"{hours_ago}.{number}",
        thread_ts="1.0",
        repo="o/r",
        pr_number=number,
        kind=kind,
        posted_at=NOW - timedelta(hours=hours_ago),
    )


class Posts:
    def __init__(self, *rows: SlackPostRow) -> None:
        self.rows = list(rows)

    def __call__(self) -> list[SlackPostRow]:
        return list(self.rows)


def tracker(posts: Posts, slug: str | None = "o/r", **kwargs) -> SlackTracker:
    return SlackTracker(
        posts=posts,
        slug_of=lambda repo_id: slug,
        bump_after=timedelta(hours=24),
        **kwargs,
    )


def test_a_pr_nobody_posted_reads_not_posted():
    t = tracker(Posts())

    assert t.context(1, "a", FETCHED, NOW) == SlackContext(posted=False)
    info = t.info(1, "a", FETCHED, NOW)
    assert info is not None
    assert (info.posted, info.last_kind, info.last_at, info.bump_due) == (
        False,
        None,
        None,
        False,
    )


def test_a_posted_pr_reports_its_last_post_and_when_a_bump_is_due():
    t = tracker(Posts(row(30), row(26, kind="bump")))

    info = t.info(1, "a", FETCHED, NOW)

    assert info is not None
    assert info.posted and info.last_kind == "bump"
    assert info.last_at == (NOW - timedelta(hours=26)).isoformat()
    assert info.bump_due
    assert t.context(1, "a", FETCHED, NOW) == SlackContext(posted=True, bump_due=True)


def test_only_the_posts_naming_this_pr_count():
    t = tracker(Posts(row(30, number=9)))

    assert t.context(1, "a", FETCHED, NOW) == SlackContext(posted=False)


def test_nothing_is_said_without_a_looked_up_pr_or_a_known_repo():
    t = tracker(Posts(row(30)))

    assert t.info(1, "a", None, NOW) is None
    assert t.info(1, "a", Fetched(status=None, at=0.0), NOW) is None
    no_pr = Fetched(status=STATUS, at=0.0, facts=None)
    assert t.context(1, "a", no_pr, NOW) is None
    assert tracker(Posts(row(30)), slug=None).context(1, "a", FETCHED, NOW) is None


def test_a_post_recorded_later_is_seen_after_a_reload():
    posts = Posts()
    t = tracker(posts)
    assert t.context(1, "a", FETCHED, NOW) == SlackContext(posted=False)

    posts.rows.append(row(1))
    assert t.context(1, "a", FETCHED, NOW) == SlackContext(posted=False)
    t.reload()

    assert t.context(1, "a", FETCHED, NOW) == SlackContext(posted=True)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def watcher(posts: Posts, clock: Clock, changes: list[set], **kwargs):
    return BumpWatcher(
        tracker=tracker(posts),
        branches=lambda: [TrackedBranch(1, "a", Path("/r"))],
        fetched=lambda repo_id, branch: FETCHED,
        wall_clock=clock,
        on_change=changes.append,
        **kwargs,
    )


def test_the_watcher_notices_a_bump_falling_due_once_a_day_has_passed():
    clock, changes = Clock(), []
    w = watcher(Posts(row(23)), clock, changes)

    assert w.check() == set()  # the first check only records
    clock.now += timedelta(minutes=30)
    assert w.check() == set()  # 23.5 hours: not yet
    clock.now += timedelta(hours=1)
    assert w.check() == {(1, "a")}  # 24.5 hours
    assert w.check() == set()  # said once


def test_the_watcher_notices_a_bump_no_longer_due():
    posts, clock, changes = Posts(row(30)), Clock(), []
    w = watcher(posts, clock, changes)
    w.check()  # due already

    posts.rows.append(row(0, kind="bump"))
    w._tracker.reload()

    assert w.check() == {(1, "a")}


def test_running_the_watcher_reports_changes_every_tick():
    clock, changes = Clock(), []
    ticks = iter([timedelta(hours=2), None])

    async def sleep(seconds: float) -> None:
        step = next(ticks)
        if step is None:
            raise asyncio.CancelledError
        clock.now += step

    w = watcher(Posts(row(23)), clock, changes, sleep=sleep, tick=60)
    w.check()  # baseline

    try:
        asyncio.run(w.run())
    except asyncio.CancelledError:
        pass

    assert changes == [{(1, "a")}]
