"""What toolbelt knows about a PR in Slack, from the posts it recorded.

A PR is *posted* once a message of toolbelt's (see ``slack_proxy``) names it,
and *bump due* when its last post or bump is older than ``bump_after`` and
nobody but its author has looked at it since the first post. "Looked" is
what GitHub shows: a review by a person, an unresolved thread a person
started, or an approval. Bots (Copilot, Actions) do not count: they are not a
reviewer answering the ask.

Pure functions of the recorded rows, the PR's facts and a time: nothing here
reads Slack or GitHub.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from toolbelt.git.stack.status import BranchStatus, ReviewState, is_bot
from toolbelt.git.stack.store import SlackPostRow
from toolbelt.ui.pr_activity import REVIEW, PrFacts

DEFAULT_BUMP_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class SlackState:
    """A PR's Slack standing. ``last_kind`` / ``last_at`` are the latest post
    or bump."""

    posted: bool
    last_kind: str | None
    last_at: datetime | None
    bump_due: bool


NOT_POSTED = SlackState(posted=False, last_kind=None, last_at=None, bump_due=False)


def someone_looked(
    facts: PrFacts | None, status: BranchStatus, since: datetime
) -> bool:
    """Whether a person other than the PR's author reviewed it, or started a
    thread on it, after ``since``."""
    if status.open_comments - status.bot_comments > 0:
        return True
    if status.review in (ReviewState.APPROVED, ReviewState.REVIEWED):
        return True
    if facts is None:
        return False
    return any(
        item.kind == REVIEW
        and item.time > since
        and item.actor != facts.author
        and not is_bot(item.actor)
        for item in facts.items
    )


def slack_state(
    posts: list[SlackPostRow],
    *,
    facts: PrFacts | None,
    status: BranchStatus,
    now: datetime,
    bump_after: timedelta = DEFAULT_BUMP_AFTER,
) -> SlackState:
    """``posts`` are the recorded rows that name this PR."""
    if not posts:
        return NOT_POSTED
    first = min(posts, key=lambda post: post.posted_at)
    last = max(posts, key=lambda post: post.posted_at)
    bump_due = now - last.posted_at >= bump_after and not someone_looked(
        facts, status, first.posted_at
    )
    return SlackState(
        posted=True, last_kind=last.kind, last_at=last.posted_at, bump_due=bump_due
    )
