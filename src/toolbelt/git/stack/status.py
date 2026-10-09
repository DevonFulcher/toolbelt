"""Per-branch PR/CI/review status for `git tree`, fetched via `gh pr view`.

One `gh pr view` per branch, run concurrently (see `stream_branch_statuses`) so
a full-stack lookup costs about one network round trip regardless of stack
size. Parsing is split out as a pure function (`_parse_status`) so it's
testable without shelling out.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import AsyncIterator

_FIELDS = "state,isDraft,reviewDecision,reviewRequests,statusCheckRollup,url,mergeable"

# `gh pr view --json` has no field for review-thread resolution, so the
# unresolved-comment count needs a raw GraphQL call instead; this pulls
# owner/repo/number out of the PR url already fetched above, rather than
# paying for a separate `gh repo view` just to learn the owner/repo.
_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")

_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes { isResolved }
      }
    }
  }
}
"""

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
    url: str | None
    open_comments: int = 0
    has_conflicts: bool = False


NO_PR = BranchStatus(
    pr=PrState.NONE, ci=CiState.NONE, review=ReviewState.NONE, url=None
)


# Widths of the longest value each field can take (excluding NONE, which is
# never shown alongside other fields — see `format_status`). Every field has a
# fixed-width slot, blank when it doesn't apply, so a field starts at the same
# column on every row no matter which other fields that row happens to have,
# including as more rows stream in with values `tree` hasn't seen yet.
_PR_WIDTH = max(len(s.value) for s in PrState if s is not PrState.NONE)
_CI_WIDTH = max(len(s.value) for s in CiState if s is not CiState.NONE)
_REVIEW_WIDTH = max(len(s.value) for s in ReviewState if s is not ReviewState.NONE)
_CONFLICTS_LABEL = "merge conflicts"
# Room for the cap of 100 threads `_REVIEW_THREADS_QUERY` fetches.
_COMMENTS_WIDTH = len("open comments: 100")


def format_status(status: BranchStatus) -> str:
    """Render a status as the `tree` suffix, e.g. "PR open    CI failed"."""
    if status.pr is PrState.NONE:
        return "PR none"
    slots = [
        f"PR {status.pr.value}".ljust(len("PR ") + _PR_WIDTH),
        f"CI {status.ci.value}".ljust(len("CI ") + _CI_WIDTH)
        if status.ci is not CiState.NONE
        else " " * (len("CI ") + _CI_WIDTH),
        f"review {status.review.value}".ljust(len("review ") + _REVIEW_WIDTH)
        if status.review is not ReviewState.NONE
        else " " * (len("review ") + _REVIEW_WIDTH),
        _CONFLICTS_LABEL if status.has_conflicts else " " * len(_CONFLICTS_LABEL),
        f"open comments: {status.open_comments}".ljust(_COMMENTS_WIDTH)
        if status.open_comments > 0
        else " " * _COMMENTS_WIDTH,
    ]
    return "  ".join(slots).rstrip()


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


def _parse_status(data: dict, *, open_comments: int = 0) -> BranchStatus:
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
        url=data["url"],
        open_comments=open_comments,
        # `mergeable` is UNKNOWN while GitHub is still computing it; only a
        # definite CONFLICTING counts, and only for a PR still in play.
        has_conflicts=data.get("mergeable") == "CONFLICTING"
        and pr_state in (PrState.OPEN, PrState.DRAFT),
    )


def _count_unresolved(payload: dict) -> int:
    """Count unresolved review threads in a `reviewThreads` GraphQL payload."""
    nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    return sum(1 for node in nodes if not node["isResolved"])


async def _fetch_open_comment_count(url: str, *, root: Path) -> int:
    """Unresolved review-thread count for the PR at `url`. Best-effort: this
    is cosmetic `tree` display, not something worth failing the whole lookup
    over, so any `gh` failure (or an unparseable url) just counts as 0."""
    match = _PR_URL_RE.search(url)
    if match is None:
        return 0
    owner, repo, number = match.groups()
    process = await asyncio.create_subprocess_exec(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_REVIEW_THREADS_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"repo={repo}",
        "-F",
        f"number={number}",
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return 0
    return _count_unresolved(json.loads(stdout))


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
    data = json.loads(stdout)
    open_comments = await _fetch_open_comment_count(data["url"], root=root)
    return _parse_status(data, open_comments=open_comments)


async def stream_branch_statuses(
    branches: list[str], *, root: Path
) -> AsyncIterator[tuple[str, BranchStatus]]:
    """Yield `(branch, status)` as each branch's lookup completes, out of order."""

    async def fetch(branch: str) -> tuple[str, BranchStatus]:
        return branch, await fetch_branch_status(branch, root=root)

    for coro in asyncio.as_completed([fetch(branch) for branch in branches]):
        yield await coro
