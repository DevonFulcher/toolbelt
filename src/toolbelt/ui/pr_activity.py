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

**CI phases.** A push starts CI, and CI takes time to run whatever its
outcome, so each commit's checks are split by ``commit_ci_phases`` into
*waiting on CI* (from the push until the commit's check suites completed;
until its end when they have not and the rollup is still pending) and, only
when it then failed, *CI failing* (from the completion until the next push or
the PR's end). The card timeline (``phaseSpans`` in ``web/src/logic/
timeline.ts``) and the aggregate replay (``aggregate.replay``) both follow
this one rule; ``web/src/testing/phaseVectors.json`` is asserted by both.
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

CI_FAILED_ROLLUPS = {"FAILURE", "ERROR"}


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
    #: For a commit: when its check suites all completed (the latest
    #: ``updatedAt``), ``None`` while unknown (still running, or no suites).
    ci_done: datetime | None = None


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
    #: Lines the PR adds and deletes against its base (0 where the source of
    #: the facts does not carry them, as in the aggregate history).
    additions: int = 0
    deletions: int = 0
    #: When the PR's base branch changed, as ``(time, previous, current)`` in
    #: time order (a stacked PR is retargeted when its parent merges).
    base_changes: tuple[tuple[datetime, str, str], ...] = ()


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
    #: For a commit: when its checks completed (see ``TimelineItem``).
    ci_done: datetime | None = None


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


def pending_reviewers(
    items: tuple[TimelineItem, ...], at: datetime | None = None
) -> tuple[str, ...]:
    """The reviewers with a request outstanding at ``at`` (default: after
    the last item), the longest waiting first. A review settles its author's
    request."""
    pending: dict[str, datetime] = {}
    for item in sorted(items, key=lambda i: i.time):
        if at is not None and item.time > at:
            break
        if item.kind == REVIEW_REQUESTED and item.subject:
            pending[item.subject] = item.time
        elif item.kind == REVIEW_REQUEST_REMOVED:
            pending.pop(item.subject, None)
        elif item.kind == REVIEW:
            pending.pop(item.subject or item.actor, None)
    return tuple(sorted(pending, key=lambda who: (pending[who], who)))


def reviewer_of(items: tuple[TimelineItem, ...], at: datetime, author: str) -> str:
    """The reviewer a task is concerned with at ``at``: the one asked for a
    review the longest ago and not yet heard from, else whoever reviewed last
    (other than the author); empty when there is none."""
    asked = pending_reviewers(items, at)
    if asked:
        return asked[0]
    reviewers = [
        item.subject or item.actor
        for item in sorted(items, key=lambda i: i.time)
        if item.kind == REVIEW
        and item.time <= at
        and (item.subject or item.actor) != author
    ]
    return reviewers[-1] if reviewers else ""


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
    failed = {oid for oid, state in facts.commit_rollups if state in CI_FAILED_ROLLUPS}
    # A force push overwrote these commits, so they are not among the PR's
    # commits any more, but they were pushed and their CI did run.
    failed |= {
        item.oid
        for item in facts.items
        if item.kind == FORCE_PUSH and item.ci in CI_FAILED_ROLLUPS
    }
    return len(failed)


def _ci_label(rollup: str | None) -> str:
    if rollup is None:
        return ""
    if rollup in CI_FAILED_ROLLUPS:
        return "failed"
    return "success" if rollup == "SUCCESS" else "pending"


@dataclass(frozen=True)
class CiPhase:
    """``kind`` (running / failing) holds from ``start`` until the next push
    (or the PR's end) unless ``end`` says when it stops."""

    kind: Literal["running", "failing"]
    start: datetime
    end: datetime | None = None


def commit_ci_phases(
    result: str, push: datetime, done: datetime | None
) -> tuple[CiPhase, ...]:
    """What a pushed commit's CI does over time, in order (``result`` is
    ``Event.ci``: ``failed``/``success``/``pending``/``""``).

    * a completion time after the push: *running* from the push until it,
      then *failing* from it when the result failed (whatever the outcome,
      CI took that long to run);
    * no completion time and still ``pending``: *running* until the next push;
    * no completion time and ``failed``: *failing* from the push. This is a
      fallback (GitHub gave no suite times), so the time CI was running is
      not separated from the time it was failing;
    * otherwise nothing (passed, or no CI).
    """
    if result == "":
        return ()
    if done is not None and done > push:
        phases = [CiPhase("running", push, done)]
        if result == "failed":
            phases.append(CiPhase("failing", done))
        return tuple(phases)
    if result == "failed":
        return (CiPhase("failing", push),)
    if result == "pending" and done is None:
        return (CiPhase("running", push),)
    return ()


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
            ci_done=item.ci_done,
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


_AGE_UNITS = (("w", 7 * 86400), ("d", 86400), ("h", 3600), ("m", 60))


def format_age(age: timedelta) -> str:
    """The largest unit and the two below it, a unit that is zero left out:
    ``5m``, ``3h 12m``, ``2d 4h 12m``, ``3w 2d 4h`` (weeks from two weeks).
    (Kept in step with ``formatElapsed`` in ``web/src/logic/format.ts``, which
    re-renders it live.)"""
    seconds = max(int(age.total_seconds()), 0)
    if seconds < 60:
        return "<1m"
    first = (
        0
        if seconds >= 14 * 86400
        else 1
        if seconds >= 86400
        else 2
        if seconds >= 3600
        else 3
    )
    parts = []
    for name, size in _AGE_UNITS[first : first + 3]:
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{name}")
    return " ".join(parts)


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_or_none(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def facts_to_dict(facts: PrFacts) -> dict:
    """A PR's facts as the JSON-ready dict ``facts_from_dict`` rebuilds them
    from (stored with the aggregate history and with each branch's last
    lookup)."""
    return {
        "number": facts.number,
        "title": facts.title,
        "author": facts.author,
        "base_ref": facts.base_ref,
        "head_oid": facts.head_oid,
        "state": facts.state,
        "is_draft": facts.is_draft,
        "created_at": facts.created_at.isoformat(),
        "merged_at": _iso_or_none(facts.merged_at),
        "closed_at": _iso_or_none(facts.closed_at),
        "requested_reviewers": list(facts.requested_reviewers),
        "commit_rollups": [list(r) for r in facts.commit_rollups],
        "additions": facts.additions,
        "deletions": facts.deletions,
        "base_changes": [
            [when.isoformat(), previous, current]
            for when, previous, current in facts.base_changes
        ],
        "items": [
            {
                "time": item.time.isoformat(),
                "kind": item.kind,
                "actor": item.actor,
                "detail": item.detail,
                "subject": item.subject,
                "oid": item.oid,
                "ci": item.ci,
                "ci_done": _iso_or_none(item.ci_done),
            }
            for item in facts.items
        ],
    }


def facts_from_dict(f: dict) -> PrFacts:
    return PrFacts(
        number=f["number"],
        title=f["title"],
        author=f["author"],
        base_ref=f["base_ref"],
        head_oid=f["head_oid"],
        state=f["state"],
        is_draft=f["is_draft"],
        created_at=datetime.fromisoformat(f["created_at"]),
        merged_at=_parse_or_none(f["merged_at"]),
        closed_at=_parse_or_none(f["closed_at"]),
        requested_reviewers=tuple(f["requested_reviewers"]),
        commit_rollups=tuple((oid, state) for oid, state in f["commit_rollups"]),
        items=tuple(
            TimelineItem(
                time=datetime.fromisoformat(item["time"]),
                kind=item["kind"],
                actor=item["actor"],
                detail=item["detail"],
                subject=item["subject"],
                oid=item["oid"],
                ci=item["ci"],
                ci_done=_parse_or_none(item["ci_done"]),
            )
            for item in f["items"]
        ),
        additions=f["additions"],
        deletions=f["deletions"],
        base_changes=tuple(
            (datetime.fromisoformat(when), previous, current)
            for when, previous, current in f.get("base_changes", [])
        ),
    )
