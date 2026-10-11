"""What a PR's history says: its ages, CI failures and event timeline.

Pure functions over ``PrFacts`` (the normalized GraphQL result, built in
``pr_fetch.py``) and a ``now``; nothing here does I/O. The derived values:

* ``pr_age``: since the PR was created. A PR that is merged or closed stops
  ageing at that moment (how long it lived).
* ``draft_age``: since the PR entered its current draft state (its last
  "converted to draft", or its creation when it was opened as a draft);
  ``None`` when it is not a draft.
* ``review_request_age``: since the oldest review request that is still
  pending. A request made before the PR was last marked ready for review only
  starts counting from then. ``None`` when no review is pending.
* ``ci_failure_count``: the number of distinct pushed commits whose CI rollup
  ended ``FAILURE``/``ERROR`` -- one per push that failed, not per failing
  job. Commits that were force-pushed away count too (they were pushed).
* ``timeline``: every event in time order.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

# Event kinds, as carried in ``TimelineItem.kind`` / ``Event.kind``.
OPENED = "opened"
READY_FOR_REVIEW = "ready_for_review"
CONVERTED_TO_DRAFT = "converted_to_draft"
REVIEW_REQUESTED = "review_requested"
REVIEW_REQUEST_REMOVED = "review_request_removed"
REVIEW = "review"
COMMIT = "commit"
FORCE_PUSH = "force_push"
MERGED = "merged"
CLOSED = "closed"
REOPENED = "reopened"

#: The event kinds above as a type, for the API's wire models (a test keeps
#: the two in step).
type ActivityKind = Literal[
    "opened",
    "ready_for_review",
    "converted_to_draft",
    "review_requested",
    "review_request_removed",
    "review",
    "commit",
    "force_push",
    "merged",
    "closed",
    "reopened",
]

_CI_FAILED_ROLLUPS = {"FAILURE", "ERROR"}


@dataclass(frozen=True)
class TimelineItem:
    """One raw timeline entry from GitHub, already flattened.

    ``subject`` is the reviewer a review request concerns (or the user a
    review was submitted by, which settles that user's pending request);
    ``oid`` is the commit it is about and ``ci`` that commit's rollup state
    (``None`` when it had no CI)."""

    time: datetime
    kind: str
    actor: str
    detail: str = ""
    subject: str = ""
    oid: str = ""
    ci: str | None = None


@dataclass(frozen=True)
class PrFacts:
    number: int
    title: str
    author: str
    base_ref: str
    head_oid: str
    #: GitHub's ``OPEN`` / ``CLOSED`` / ``MERGED``.
    state: str
    is_draft: bool
    created_at: datetime
    merged_at: datetime | None
    closed_at: datetime | None
    #: Reviewers (login or team) with a request outstanding right now.
    requested_reviewers: tuple[str, ...]
    #: ``(oid, rollup state)`` of the PR's recent commits; state ``None``
    #: when the commit had no CI.
    commit_rollups: tuple[tuple[str, str | None], ...]
    items: tuple[TimelineItem, ...]


@dataclass(frozen=True)
class Event:
    time: datetime
    kind: str
    actor: str
    detail: str
    #: For a commit (or the commit a force push replaced): ``failed``,
    #: ``success``, ``pending``, or ``""`` when it had no CI.
    ci: str = ""
    #: The reviewer a review request concerns, or who submitted a review.
    subject: str = ""


@dataclass(frozen=True)
class PrActivity:
    pr_age: timedelta
    draft_age: timedelta | None
    #: When the draft state / the oldest pending review request began (the
    #: ages are ``now`` minus these).
    draft_since: datetime | None
    review_request_age: timedelta | None
    review_request_since: datetime | None
    ci_failure_count: int
    timeline: tuple[Event, ...]


def _is_done(facts: PrFacts) -> bool:
    return facts.state in ("MERGED", "CLOSED")


def _pr_age(facts: PrFacts, now: datetime) -> timedelta:
    end = now
    if facts.state == "MERGED" and facts.merged_at is not None:
        end = facts.merged_at
    elif facts.state == "CLOSED" and facts.closed_at is not None:
        end = facts.closed_at
    return max(end - facts.created_at, timedelta(0))


def _latest(items: tuple[TimelineItem, ...], kind: str) -> datetime | None:
    times = [item.time for item in items if item.kind == kind]
    return max(times) if times else None


def _draft_since(facts: PrFacts) -> datetime | None:
    if not facts.is_draft or _is_done(facts):
        return None
    return _latest(facts.items, CONVERTED_TO_DRAFT) or facts.created_at


def _review_request_since(facts: PrFacts) -> datetime | None:
    if _is_done(facts):
        return None
    # The PR's own list of outstanding requests is authoritative; the
    # timeline only dates them (it may be truncated for a very busy PR, in
    # which case that reviewer's request is simply undated).
    ready_since = _latest(facts.items, READY_FOR_REVIEW)
    since: list[datetime] = []
    for reviewer in facts.requested_reviewers:
        requested = [
            item.time
            for item in facts.items
            if item.kind == REVIEW_REQUESTED and item.subject == reviewer
        ]
        if not requested:
            continue
        started = max(requested)
        if not facts.is_draft and ready_since and ready_since > started:
            started = ready_since
        since.append(started)
    return min(since) if since else None


def _ci_failure_count(facts: PrFacts) -> int:
    failed = {oid for oid, state in facts.commit_rollups if state in _CI_FAILED_ROLLUPS}
    # A force push overwrote these commits, so they are not among the PR's
    # commits any more, but they were pushed and their CI did run.
    failed |= {
        item.oid
        for item in facts.items
        if item.kind == FORCE_PUSH and item.ci in _CI_FAILED_ROLLUPS
    }
    return len(failed)


def _ci_label(rollup: str | None) -> str:
    if rollup is None:
        return ""
    if rollup in _CI_FAILED_ROLLUPS:
        return "failed"
    return "success" if rollup == "SUCCESS" else "pending"


def _timeline(facts: PrFacts) -> tuple[Event, ...]:
    opened = Event(
        time=facts.created_at,
        kind=OPENED,
        actor=facts.author,
        detail=facts.title,
    )
    events = [opened] + [
        Event(
            time=item.time,
            kind=item.kind,
            actor=item.actor,
            detail=item.detail,
            ci=_ci_label(item.ci),
            subject=item.subject,
        )
        for item in facts.items
    ]
    # Stable: events sharing a timestamp keep GitHub's order, with "opened"
    # first.
    return tuple(sorted(events, key=lambda event: event.time))


def _since(start: datetime | None, now: datetime) -> timedelta | None:
    return None if start is None else max(now - start, timedelta(0))


def derive_activity(facts: PrFacts, now: datetime) -> PrActivity:
    draft_since = _draft_since(facts)
    review_since = _review_request_since(facts)
    return PrActivity(
        pr_age=_pr_age(facts, now),
        draft_age=_since(draft_since, now),
        draft_since=draft_since,
        review_request_age=_since(review_since, now),
        review_request_since=review_since,
        ci_failure_count=_ci_failure_count(facts),
        timeline=_timeline(facts),
    )


def format_age(age: timedelta) -> str:
    """Compact: ``5m``, ``3h``, ``4d``, ``3w``. (Kept in step with
    ``formatAge`` in ``web/src/logic/format.ts``, which re-renders it live.)"""
    seconds = max(int(age.total_seconds()), 0)
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 14 * 86400:
        return f"{seconds // 86400}d"
    return f"{seconds // (7 * 86400)}w"
