"""Per-branch PR/CI/review status for `git tree`, fetched via `gh pr view`.

One `gh pr view` per branch, run concurrently (see `stream_branch_statuses`) so
a full-stack lookup costs about one network round trip regardless of stack
size. Parsing is split out as a pure function (`_parse_status`) so it's
testable without shelling out.
"""

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import AsyncIterator

_FIELDS = "state,isDraft,reviewDecision,reviewRequests,statusCheckRollup"

# Check-run/status-context outcomes that mean the run did not pass. Anything
# else completed (SUCCESS, NEUTRAL, SKIPPED, STALE) counts as passing for our
# purposes — there's no "unknown" bucket in the three CI states we show.
_CI_FAILING = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED"}
_CI_RUNNING = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING"}


class PrState(str, Enum):
    NONE = "none"
    DRAFT = "draft"
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class CiState(str, Enum):
    NONE = "none"
    RUNNING = "running"
    FAILED = "failed"
    SUCCESS = "success"


class ReviewState(str, Enum):
    NONE = "none"
    REQUESTED = "requested"
    APPROVED = "approved"
    REVIEWED = "reviewed"


@dataclass(frozen=True)
class BranchStatus:
    pr: PrState
    ci: CiState
    review: ReviewState


NO_PR = BranchStatus(pr=PrState.NONE, ci=CiState.NONE, review=ReviewState.NONE)


# Widths of the longest value each field can take (excluding NONE, which is
# never shown alongside other fields — see `format_status`). Padding to these
# fixed widths, rather than each row's own longest value, keeps a field
# (e.g. "CI") starting at the same column on every row, including as more
# rows stream in with values `tree` hasn't seen yet.
_PR_WIDTH = max(len(s.value) for s in PrState if s is not PrState.NONE)
_CI_WIDTH = max(len(s.value) for s in CiState if s is not CiState.NONE)
_REVIEW_WIDTH = max(len(s.value) for s in ReviewState if s is not ReviewState.NONE)


def format_status(status: BranchStatus) -> str:
    """Render a status as the `tree` suffix, e.g. "PR open    CI failed"."""
    if status.pr is PrState.NONE:
        return "PR none"
    fields = [("PR", status.pr.value, _PR_WIDTH)]
    if status.ci is not CiState.NONE:
        fields.append(("CI", status.ci.value, _CI_WIDTH))
    if status.review is not ReviewState.NONE:
        fields.append(("review", status.review.value, _REVIEW_WIDTH))
    last = len(fields) - 1
    parts = [
        f"{label} {value}" if i == last else f"{label} {value.ljust(width)}"
        for i, (label, value, width) in enumerate(fields)
    ]
    return "  ".join(parts)


def _ci_state(rollup: list[dict]) -> CiState:
    if not rollup:
        return CiState.NONE
    outcomes = {
        entry.get("conclusion") or entry.get("state") or entry.get("status")
        for entry in rollup
    }
    if outcomes & _CI_FAILING:
        return CiState.FAILED
    if outcomes & _CI_RUNNING:
        return CiState.RUNNING
    return CiState.SUCCESS


def _parse_status(data: dict) -> BranchStatus:
    """Parse a `gh pr view --json {_FIELDS}` payload into a `BranchStatus`."""
    if data["state"] == "MERGED":
        pr_state = PrState.MERGED
    elif data["state"] == "CLOSED":
        pr_state = PrState.CLOSED
    elif data["isDraft"]:
        pr_state = PrState.DRAFT
    else:
        pr_state = PrState.OPEN

    decision = data.get("reviewDecision") or ""
    if decision == "APPROVED":
        review_state = ReviewState.APPROVED
    elif decision == "CHANGES_REQUESTED":
        review_state = ReviewState.REVIEWED
    elif data.get("reviewRequests"):
        review_state = ReviewState.REQUESTED
    else:
        review_state = ReviewState.NONE

    return BranchStatus(
        pr=pr_state,
        ci=_ci_state(data.get("statusCheckRollup") or []),
        review=review_state,
    )


async def fetch_branch_status(branch: str, *, root: Path) -> BranchStatus:
    """Look up `branch`'s PR/CI/review status. No PR at all is `NO_PR`."""
    process = await asyncio.create_subprocess_exec(
        "gh",
        "pr",
        "view",
        branch,
        "--json",
        _FIELDS,
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        # `gh pr view` exits non-zero when the branch has no PR — that's the
        # expected, common case here, not an infra failure to guard against.
        return NO_PR
    return _parse_status(json.loads(stdout))


async def stream_branch_statuses(
    branches: list[str], *, root: Path
) -> AsyncIterator[tuple[str, BranchStatus]]:
    """Yield `(branch, status)` as each branch's lookup completes, out of order."""

    async def fetch(branch: str) -> tuple[str, BranchStatus]:
        return branch, await fetch_branch_status(branch, root=root)

    for coro in asyncio.as_completed([fetch(branch) for branch in branches]):
        yield await coro
