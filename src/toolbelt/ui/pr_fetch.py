"""The UI's one-call-per-poll PR lookup.

``fetch_snapshot`` asks GitHub for everything the UI shows about a branch's PR
in a single ``gh api graphql`` call: the same state `tt g tree` prints (parsed
by ``status.parse_status``, so the two cannot disagree on what "CI failed"
means) plus the review threads, and the timeline and per-commit CI that the
ages and event feed are derived from (``pr_activity``). That replaces the two
calls (``gh pr view`` and a review-threads query) a status lookup costs `tt g
tree`; `tt g tree` itself is unchanged.

The PR is found by head branch name within the checkout's own repository
(``gh`` fills ``{owner}``/``{repo}`` from the checkout), newest first, so a
branch whose earlier PR was closed shows the current attempt.

The call is a seam (``GraphqlRunner``) so tests inject canned responses.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from toolbelt import logged_process
from toolbelt.git.stack.status import (
    NO_PR,
    BranchStatus,
    MERGEABLE_RETRIES,
    MERGEABLE_RETRY_DELAY_SECONDS,
    count_unresolved_nodes,
    parse_status,
)
from toolbelt.ui import pr_activity
from toolbelt.ui.pr_activity import PrFacts, TimelineItem

# How many of the PR's most recent commits / timeline entries are looked at.
# Beyond these a very long-lived PR's oldest history is simply not shown.
_COMMITS = 30
_TIMELINE_ITEMS = 100

_TIMELINE_TYPES = (
    "READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, REVIEW_REQUESTED_EVENT, "
    "REVIEW_REQUEST_REMOVED_EVENT, PULL_REQUEST_REVIEW, PULL_REQUEST_COMMIT, "
    "HEAD_REF_FORCE_PUSHED_EVENT, MERGED_EVENT, CLOSED_EVENT, REOPENED_EVENT"
)

_REVIEWER = "requestedReviewer { __typename ... on User { login } ... on Team { name } ... on Mannequin { login } }"

PR_QUERY = f"""
query($owner: String!, $repo: String!, $branch: String!) {{
  repository(owner: $owner, name: $repo) {{
    pullRequests(headRefName: $branch, first: 1,
                 orderBy: {{field: CREATED_AT, direction: DESC}}) {{
      nodes {{
        number url title state isDraft createdAt mergedAt closedAt
        headRefOid baseRefName mergeable reviewDecision
        author {{ login }}
        reviewRequests(first: 20) {{ nodes {{ {_REVIEWER} }} }}
        reviewThreads(first: 100) {{ nodes {{ isResolved }} }}
        head: commits(last: 1) {{ nodes {{ commit {{
          statusCheckRollup {{ state contexts(first: 100) {{ nodes {{
            __typename
            ... on CheckRun {{ conclusion status }}
            ... on StatusContext {{ state }}
          }} }} }}
        }} }} }}
        recent: commits(last: {_COMMITS}) {{ nodes {{ commit {{
          oid statusCheckRollup {{ state }}
        }} }} }}
        timelineItems(last: {_TIMELINE_ITEMS}, itemTypes: [{_TIMELINE_TYPES}]) {{
          nodes {{
            __typename
            ... on ReadyForReviewEvent {{ createdAt actor {{ login }} }}
            ... on ConvertToDraftEvent {{ createdAt actor {{ login }} }}
            ... on ReviewRequestedEvent {{ createdAt actor {{ login }} {_REVIEWER} }}
            ... on ReviewRequestRemovedEvent {{ createdAt actor {{ login }} {_REVIEWER} }}
            ... on PullRequestReview {{ submittedAt state author {{ login }} }}
            ... on PullRequestCommit {{ commit {{
              oid messageHeadline committedDate
              author {{ user {{ login }} name }}
              statusCheckRollup {{ state }}
            }} }}
            ... on HeadRefForcePushedEvent {{
              createdAt actor {{ login }}
              beforeCommit {{ oid statusCheckRollup {{ state }} }}
              afterCommit {{ oid }}
            }}
            ... on MergedEvent {{ createdAt actor {{ login }} }}
            ... on ClosedEvent {{ createdAt actor {{ login }} }}
            ... on ReopenedEvent {{ createdAt actor {{ login }} }}
          }}
        }}
      }}
    }}
  }}
}}
"""

#: (branch, repo checkout) -> the GraphQL response (``{"data": ...}``).
GraphqlRunner = Callable[[str, Path], Awaitable[dict]]


@dataclass(frozen=True)
class PrSnapshot:
    """Everything one lookup learned. ``facts`` is ``None`` without a PR."""

    status: BranchStatus
    facts: PrFacts | None


NO_SNAPSHOT = PrSnapshot(status=NO_PR, facts=None)


async def gh_graphql(branch: str, root: Path) -> dict:
    process = await logged_process.create_subprocess_exec(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={PR_QUERY}",
        "-F",
        "owner={owner}",
        "-F",
        "repo={repo}",
        "-f",
        f"branch={branch}",
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        # Unlike `gh pr view`, a GraphQL lookup of "no PR" succeeds, so a
        # failure here is a real one (auth, network): show it, don't hide it.
        raise RuntimeError(f"gh api graphql failed: {stderr.decode().strip()}")
    return json.loads(stdout)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value else None


def _login(node: dict | None) -> str:
    return (node or {}).get("login") or ""


def _reviewer(node: dict | None) -> str:
    node = node or {}
    return node.get("login") or node.get("name") or ""


def _rollup_state(commit: dict | None) -> str | None:
    rollup = (commit or {}).get("statusCheckRollup")
    return rollup["state"] if rollup else None


_REVIEW_DETAIL = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "requested changes",
    "COMMENTED": "commented",
    "DISMISSED": "review dismissed",
}


def _item(node: dict) -> TimelineItem | None:
    kind = node["__typename"]
    if kind == "ReadyForReviewEvent":
        return TimelineItem(
            _parse_time(node["createdAt"]),
            pr_activity.READY_FOR_REVIEW,
            _login(node["actor"]),
            "marked ready for review",
        )
    if kind == "ConvertToDraftEvent":
        return TimelineItem(
            _parse_time(node["createdAt"]),
            pr_activity.CONVERTED_TO_DRAFT,
            _login(node["actor"]),
            "converted to draft",
        )
    if kind in ("ReviewRequestedEvent", "ReviewRequestRemovedEvent"):
        who = _reviewer(node.get("requestedReviewer"))
        requested = kind == "ReviewRequestedEvent"
        return TimelineItem(
            _parse_time(node["createdAt"]),
            pr_activity.REVIEW_REQUESTED
            if requested
            else pr_activity.REVIEW_REQUEST_REMOVED,
            _login(node["actor"]),
            f"requested review from {who}"
            if requested
            else f"removed review request for {who}",
            subject=who,
        )
    if kind == "PullRequestReview":
        time = _parse_optional_time(node.get("submittedAt"))
        if time is None:  # a pending (unsubmitted) review
            return None
        reviewer = _login(node["author"])
        return TimelineItem(
            time,
            pr_activity.REVIEW,
            reviewer,
            _REVIEW_DETAIL.get(node["state"], node["state"].lower()),
            subject=reviewer,
        )
    if kind == "PullRequestCommit":
        commit = node["commit"]
        author = commit.get("author") or {}
        return TimelineItem(
            _parse_time(commit["committedDate"]),
            pr_activity.COMMIT,
            _login(author.get("user")) or author.get("name") or "",
            f"{commit['oid'][:7]} {commit['messageHeadline']}",
            oid=commit["oid"],
            ci=_rollup_state(commit),
        )
    if kind == "HeadRefForcePushedEvent":
        before = node.get("beforeCommit") or {}
        after = node.get("afterCommit") or {}
        return TimelineItem(
            _parse_time(node["createdAt"]),
            pr_activity.FORCE_PUSH,
            _login(node["actor"]),
            f"force-pushed {before.get('oid', '')[:7]} to {after.get('oid', '')[:7]}",
            oid=before.get("oid", ""),
            ci=_rollup_state(before),
        )
    if kind == "MergedEvent":
        return TimelineItem(
            _parse_time(node["createdAt"]),
            pr_activity.MERGED,
            _login(node["actor"]),
            "merged",
        )
    if kind == "ClosedEvent":
        return TimelineItem(
            _parse_time(node["createdAt"]),
            pr_activity.CLOSED,
            _login(node["actor"]),
            "closed",
        )
    if kind == "ReopenedEvent":
        return TimelineItem(
            _parse_time(node["createdAt"]),
            pr_activity.REOPENED,
            _login(node["actor"]),
            "reopened",
        )
    return None


def _check_entries(node: dict) -> list[dict]:
    """The head commit's check contexts, shaped like `gh pr view`'s
    ``statusCheckRollup`` entries (which is what ``parse_status`` reads)."""
    commits = node["head"]["nodes"]
    if not commits:
        return []
    rollup = commits[0]["commit"]["statusCheckRollup"]
    if not rollup:
        return []
    return [
        {
            "conclusion": context.get("conclusion"),
            "status": context.get("status"),
            "state": context.get("state"),
        }
        for context in rollup["contexts"]["nodes"]
    ]


def timeline_items(nodes: list[dict]) -> tuple[TimelineItem, ...]:
    """A PR's ``timelineItems`` nodes as ``TimelineItem``s in time order
    (entries of kinds the UI does not use are skipped)."""
    parsed = [item for item in (_item(entry) for entry in nodes) if item is not None]
    # Merging a PR also records a ClosedEvent at the same instant: that is not
    # a PR closed without merging, so it is not a second ending.
    merged_at = {item.time for item in parsed if item.kind == pr_activity.MERGED}
    return tuple(
        sorted(
            (
                item
                for item in parsed
                if not (item.kind == pr_activity.CLOSED and item.time in merged_at)
            ),
            key=lambda item: item.time,
        )
    )


def parse_snapshot(payload: dict) -> PrSnapshot:
    """Parse a ``PR_QUERY`` response."""
    nodes = payload["data"]["repository"]["pullRequests"]["nodes"]
    if not nodes:
        return NO_SNAPSHOT
    node = nodes[0]
    reviewers = tuple(
        _reviewer(request["requestedReviewer"])
        for request in node["reviewRequests"]["nodes"]
    )
    status = parse_status(
        {
            "state": node["state"],
            "isDraft": node["isDraft"],
            "reviewDecision": node["reviewDecision"],
            "reviewRequests": list(reviewers),
            "statusCheckRollup": _check_entries(node),
            "url": node["url"],
            "mergeable": node["mergeable"],
        },
        open_comments=count_unresolved_nodes(node["reviewThreads"]["nodes"]),
    )
    items = timeline_items(node["timelineItems"]["nodes"])
    facts = PrFacts(
        number=node["number"],
        title=node["title"],
        author=_login(node["author"]),
        base_ref=node["baseRefName"],
        head_oid=node["headRefOid"],
        state=node["state"],
        is_draft=node["isDraft"],
        created_at=_parse_time(node["createdAt"]),
        merged_at=_parse_optional_time(node["mergedAt"]),
        closed_at=_parse_optional_time(node["closedAt"]),
        requested_reviewers=reviewers,
        commit_rollups=tuple(
            (entry["commit"]["oid"], _rollup_state(entry["commit"]))
            for entry in node["recent"]["nodes"]
        ),
        items=items,
    )
    return PrSnapshot(status=status, facts=facts)


def _mergeable_unknown(payload: dict) -> bool:
    nodes = payload["data"]["repository"]["pullRequests"]["nodes"]
    return (
        bool(nodes)
        and nodes[0]["state"] == "OPEN"
        and nodes[0]["mergeable"] == "UNKNOWN"
    )


async def fetch_snapshot(
    branch: str,
    root: Path,
    *,
    graphql: GraphqlRunner = gh_graphql,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PrSnapshot:
    """One snapshot of ``branch``'s PR. GitHub reports ``mergeable: UNKNOWN``
    for a few seconds after the base or head moves, so that case is retried
    (rarely: it is the only time this costs more than one call)."""
    payload = await graphql(branch, root)
    for _ in range(MERGEABLE_RETRIES):
        if not _mergeable_unknown(payload):
            break
        await sleep(MERGEABLE_RETRY_DELAY_SECONDS)
        payload = await graphql(branch, root)
    return parse_snapshot(payload)
