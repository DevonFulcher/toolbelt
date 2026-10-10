"""A branch's PR status split into the UI table's separate columns.

Each status column carries two strings the page's sort/filter script reads
(``static/stack-table.js``): ``value``, matched exactly by the column's filter
dropdown, and ``sort``, compared numerically when both sides are numbers.
Both are ``""`` while the status is still loading, and ``sort`` is ``""``
where there is nothing to order by (always sorted last).
"""

import re
from dataclasses import dataclass

from toolbelt.git.stack.status import (
    BranchStatus,
    CiState,
    PrState,
    ReviewState,
)

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")

# Orders the non-"none" states from most to least urgent.
_CI_RANK = {CiState.FAILED: 1, CiState.RUNNING: 2, CiState.SUCCESS: 3}
_REVIEW_RANK = {
    ReviewState.REQUESTED: 1,
    ReviewState.REVIEWED: 2,
    ReviewState.APPROVED: 3,
}
MERGE_CONFLICTS = "conflicts"
MERGE_CLEAN = "clean"
_MERGE_RANK = {MERGE_CONFLICTS: 1, MERGE_CLEAN: 2}

# Filter dropdown options, by column, in display order.
FILTER_OPTIONS: dict[str, list[str]] = {
    "pr": [s.value for s in PrState],
    "ci": [s.value for s in CiState],
    "review": [s.value for s in ReviewState],
    "merge": [MERGE_CONFLICTS, MERGE_CLEAN, "none"],
}


@dataclass(frozen=True)
class StatusCells:
    #: False until a result is in (or when there is no checkout to ask).
    loaded: bool
    pr: str
    pr_sort: str
    pr_number: str
    pr_url: str | None
    ci: str
    ci_sort: str
    review: str
    review_sort: str
    open_comments: int
    merge: str
    merge_sort: str


UNLOADED = StatusCells(
    loaded=False,
    pr="",
    pr_sort="",
    pr_number="",
    pr_url=None,
    ci="",
    ci_sort="",
    review="",
    review_sort="",
    open_comments=0,
    merge="",
    merge_sort="",
)


def status_cells(status: BranchStatus | None) -> StatusCells:
    if status is None:
        return UNLOADED
    match = _PR_NUMBER_RE.search(status.url) if status.url else None
    number = match.group(1) if match else ""
    has_pr = status.pr is not PrState.NONE
    merge = "none"
    if status.pr in (PrState.OPEN, PrState.DRAFT):
        merge = MERGE_CONFLICTS if status.has_conflicts else MERGE_CLEAN
    return StatusCells(
        loaded=True,
        pr=status.pr.value,
        pr_sort=number if has_pr else "",
        pr_number=number if has_pr else "",
        pr_url=status.url if has_pr else None,
        ci=status.ci.value,
        ci_sort=str(_CI_RANK.get(status.ci, "")),
        review=status.review.value,
        review_sort=str(_REVIEW_RANK.get(status.review, "")),
        open_comments=status.open_comments if has_pr else 0,
        merge=merge,
        merge_sort=str(_MERGE_RANK.get(merge, "")),
    )
