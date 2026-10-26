"""What toolbelt knows about a PR in Slack: posted, bump due, someone looked."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import SlackPostRow
from toolbelt.ui import slack_posts
from toolbelt.ui.pr_activity import PrFacts, TimelineItem

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
URL = "https://github.com/o/r/pull/5"


def post(hours_ago: float, kind: str = "post") -> SlackPostRow:
    return SlackPostRow(
        channel="C1",
        ts=f"{hours_ago}",
        thread_ts="1",
        repo="o/r",
        pr_number=5,
        kind=kind,
        posted_at=NOW - timedelta(hours=hours_ago),
    )


def status(**overrides) -> BranchStatus:
    base = {
        "pr": PrState.OPEN,
        "ci": CiState.SUCCESS,
        "review": ReviewState.REQUESTED,
        "url": URL,
    }
    base.update(overrides)
    return BranchStatus(**base)  # type: ignore[arg-type]


def facts(*items: TimelineItem, author: str = "devon") -> PrFacts:
    return PrFacts(
        number=5,
        title="Add it",
        author=author,
        base_ref="main",
        head_oid="abc",
        state="OPEN",
        is_draft=False,
        created_at=NOW - timedelta(days=3),
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=items,
    )


def review(hours_ago: float, actor: str) -> TimelineItem:
    return TimelineItem(
        time=NOW - timedelta(hours=hours_ago),
        kind="review",
        actor=actor,
        detail="commented",
    )


def state(posts, *, facts_=None, status_=None, **kwargs):
    return slack_posts.slack_state(
        posts, facts=facts_, status=status_ or status(), now=NOW, **kwargs
    )


def test_a_pr_with_no_posts_is_not_posted():
    assert state([]) == slack_posts.NOT_POSTED


def test_the_latest_post_or_bump_is_when_it_was_last_asked():
    got = state([post(30), post(5, "bump")])

    assert got.posted and got.last_kind == "bump"
    assert got.last_at == NOW - timedelta(hours=5)
    assert not got.bump_due  # asked 5 hours ago


def test_a_bump_is_due_a_day_after_the_last_ask_when_nobody_looked():
    assert state([post(25)]).bump_due
    assert not state([post(23)]).bump_due
    # A bump restarts the day.
    assert not state([post(60), post(10, "bump")]).bump_due


FRIDAY_5PM = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)


def posted_on_friday(now: datetime):
    """A post made on Friday at 5pm, looked at on ``now``."""
    row = replace(post(0), posted_at=FRIDAY_5PM)
    return slack_posts.slack_state(
        [row], facts=None, status=status(), now=now, zone=UTC
    )


def test_a_weekend_does_not_count_towards_the_wait_before_a_bump():
    assert not posted_on_friday(
        datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
    ).bump_due  # Saturday
    assert not posted_on_friday(
        datetime(2026, 10, 25, 23, 0, tzinfo=UTC)
    ).bump_due  # Sunday
    assert not posted_on_friday(
        datetime(2026, 10, 26, 16, 0, tzinfo=UTC)
    ).bump_due  # Monday 4pm
    assert posted_on_friday(
        datetime(2026, 10, 26, 17, 0, tzinfo=UTC)
    ).bump_due  # Monday 5pm


def test_the_wait_before_a_bump_is_configurable():
    assert state([post(5)], bump_after=timedelta(hours=4)).bump_due


def test_a_review_by_a_person_after_the_first_post_means_no_bump():
    looked = facts(review(10, "alice"))

    assert not state([post(30)], facts_=looked).bump_due


def test_a_review_before_the_first_post_does_not_count():
    earlier = facts(review(40, "alice"))

    assert state([post(30)], facts_=earlier).bump_due


def test_the_authors_own_review_and_bots_do_not_count():
    own_and_bots = facts(
        review(10, "devon"),
        review(9, "copilot-pull-request-reviewer"),
        review(8, "github-actions[bot]"),
    )

    assert state([post(30)], facts_=own_and_bots).bump_due


def test_a_thread_a_person_started_or_an_approval_means_no_bump():
    assert not state([post(30)], status_=status(open_comments=1)).bump_due
    assert not state([post(30)], status_=status(review=ReviewState.APPROVED)).bump_due
    assert not state([post(30)], status_=status(review=ReviewState.REVIEWED)).bump_due


def test_a_thread_only_a_bot_started_does_not_count():
    assert state([post(30)], status_=status(open_comments=2, bot_comments=2)).bump_due
