"""A branch's PR status split into the UI table's separate columns.

Each status column carries two strings the page's sort/filter script reads
(``static/stack-table.js``): ``value``, matched exactly by the column's filter
dropdown, and ``sort``, compared numerically when both sides are numbers.
Both are ``""`` while the status is still loading (or failed), and ``sort`` is
``""`` where there is nothing to order by (always sorted last).

The "next" column holds the branch's ``next_action``: its value is who the
branch is waiting on (``you``/``ci``/``others``/``none``) and its sort ranks
those so rows that need the user come first. The richer per-PR history (ages,
CI failures, event feed) rides along as ``detail`` for the cards and their per-card timelines.
"""

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from toolbelt.git.stack.status import (
    BranchStatus,
    CiState,
    PrState,
    ReviewState,
)
from toolbelt.ui import pr_states
from toolbelt.ui.next_action import WHO_ORDER, ParentContext, next_action
from toolbelt.ui.pr_activity import PrFacts, derive_activity, format_age

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")

# Orders the non-"none" states from most to least urgent.
_CI_RANK = {CiState.FAILED: 1, CiState.RUNNING: 2, CiState.SUCCESS: 3}
_REVIEW_RANK = {
    ReviewState.REQUESTED: 1,
    ReviewState.REVIEWED: 2,
    ReviewState.APPROVED: 3,
}
COMMENTS_OPEN = "has comments"
COMMENTS_NONE = "none"
MERGE_CONFLICTS = "conflicts"
MERGE_CLEAN = "clean"
_MERGE_RANK = {MERGE_CONFLICTS: 1, MERGE_CLEAN: 2}

# Filter dropdown options, by column, in display order.
FILTER_OPTIONS: dict[str, list[str]] = {
    "next": list(WHO_ORDER),
    "pr": [s.value for s in PrState],
    "ci": [s.value for s in CiState],
    "review": [s.value for s in ReviewState],
    "comments": [COMMENTS_OPEN, COMMENTS_NONE],
    "merge": [MERGE_CONFLICTS, MERGE_CLEAN, "none"],
}


@dataclass(frozen=True)
class Age:
    """A duration for display. ``since`` (ISO time) is set while it is still
    growing, so the page can keep the text current; ``None`` once it ended."""

    text: str
    since: str | None


@dataclass(frozen=True)
class PrDetail:
    title: str
    pr_age: Age
    draft_age: Age | None
    review_request_age: Age | None
    ci_failures: int
    #: Reviewers with a review request outstanding.
    reviewers: tuple[str, ...]
    #: ISO time the PR was created, when it ended (merged/closed; ``None``
    #: while open) and whether it is a draft: what the per-card timeline
    #: track is scaled and shaded from.
    created: str
    ended: str | None
    is_draft: bool
    #: ``[{t, kind, actor, detail, ci, subject}]`` in time order, times as ISO strings.
    events: list[dict]


@dataclass(frozen=True)
class StatusCells:
    #: False until a result is in (or when there is no checkout to ask).
    loaded: bool
    #: True when the lookup was attempted and failed (``loaded`` is False).
    failed: bool
    pr: str
    pr_sort: str
    pr_number: str
    pr_url: str | None
    ci: str
    ci_sort: str
    review: str
    review_sort: str
    open_comments: int
    comments: str
    comments_sort: str
    merge: str
    merge_sort: str
    next_label: str = ""
    next_who: str = ""
    next_kind: str = ""
    next_sort: str = ""
    detail: PrDetail | None = None
    #: The "now" chips of the card's timeline (``pr_states.current_states``).
    states: tuple[dict, ...] = ()


UNLOADED = StatusCells(
    loaded=False,
    failed=False,
    pr="",
    pr_sort="",
    pr_number="",
    pr_url=None,
    ci="",
    ci_sort="",
    review="",
    review_sort="",
    open_comments=0,
    comments="",
    comments_sort="",
    merge="",
    merge_sort="",
)

FAILED = replace(UNLOADED, failed=True)


def _iso(time: datetime) -> str:
    return time.isoformat().replace("+00:00", "Z")


def _age(age: timedelta | None, since: datetime | None) -> Age | None:
    """A growing age: the page keeps its text current from ``since``."""
    if age is None or since is None:
        return None
    return Age(text=format_age(age), since=_iso(since))


def pr_detail(facts: PrFacts, now: datetime) -> PrDetail:
    activity = derive_activity(facts, now)
    done = facts.state in ("MERGED", "CLOSED")
    ended = None
    if facts.state == "MERGED":
        ended = facts.merged_at
    elif facts.state == "CLOSED":
        ended = facts.closed_at
    return PrDetail(
        title=facts.title,
        # A finished PR's age is fixed, so it carries no start to count from.
        pr_age=Age(
            text=format_age(activity.pr_age),
            since=None if done else _iso(facts.created_at),
        ),
        draft_age=_age(activity.draft_age, activity.draft_since),
        review_request_age=_age(
            activity.review_request_age, activity.review_request_since
        ),
        ci_failures=activity.ci_failure_count,
        reviewers=facts.requested_reviewers,
        created=_iso(facts.created_at),
        ended=_iso(ended) if ended else None,
        is_draft=facts.is_draft,
        events=[
            {
                "t": _iso(event.time),
                "kind": event.kind,
                "actor": event.actor,
                "detail": event.detail,
                "ci": event.ci,
                "subject": event.subject,
            }
            for event in activity.timeline
        ],
    )


def status_cells(
    status: BranchStatus | None,
    *,
    failed: bool = False,
    parent: ParentContext | None = None,
    facts: PrFacts | None = None,
    now: datetime | None = None,
) -> StatusCells:
    """``status`` is ``None`` while loading, or when ``failed``. ``parent``
    is the stack context for the next action; ``facts`` (with ``now``) adds
    the PR's history as ``detail``."""
    if status is None:
        return FAILED if failed else UNLOADED
    match = _PR_NUMBER_RE.search(status.url) if status.url else None
    number = match.group(1) if match else ""
    has_pr = status.pr is not PrState.NONE
    merge = "none"
    if status.pr in (PrState.OPEN, PrState.DRAFT):
        merge = MERGE_CONFLICTS if status.has_conflicts else MERGE_CLEAN
    open_comments = status.open_comments if has_pr else 0
    action = next_action(status, parent)
    detail = pr_detail(facts, now) if facts is not None and now is not None else None
    return StatusCells(
        loaded=True,
        failed=False,
        pr=status.pr.value,
        pr_sort=number if has_pr else "",
        pr_number=number if has_pr else "",
        pr_url=status.url if has_pr else None,
        ci=status.ci.value,
        ci_sort=str(_CI_RANK.get(status.ci, "")),
        review=status.review.value,
        review_sort=str(_REVIEW_RANK.get(status.review, "")),
        open_comments=open_comments,
        comments=COMMENTS_OPEN if open_comments else COMMENTS_NONE,
        comments_sort=str(open_comments) if has_pr else "",
        merge=merge,
        merge_sort=str(_MERGE_RANK.get(merge, "")),
        next_label=action.label,
        next_who=action.who,
        next_kind=action.kind,
        next_sort=str(WHO_ORDER.index(action.who) + 1),
        detail=detail,
        states=tuple(
            pr_states.current_states(
                pr=status.pr.value,
                ci=status.ci.value,
                review=status.review.value,
                open_comments=open_comments,
                merge=merge,
                next_kind=action.kind,
                next_who=action.who,
                next_label=action.label,
            )
        ),
    )
