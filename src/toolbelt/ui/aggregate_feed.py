"""Where the aggregate view's inputs come from, and their cache.

``GET /api/aggregate`` needs two kinds of history:

* **GitHub**: the user's PRs that were updated in the window (every state)
  plus every PR still open (a quiet open PR is in flight for the whole window
  but was not "updated"), each with the timeline items the card timeline
  uses. Paginated GraphQL searches (``author:@me``, archived repos left out),
  fetched when the view is opened and cached for ``DEFAULT_TTL_SECONDS``
  (merged PRs are immutable, open ones change slowly at this scale). A larger
  cached window serves a smaller one. A page that fails after the first, or
  an organization the token is not authorized for, yields a partial result
  that is flagged, as the untracked-PR callout does.
* **The stack DB**: tracked and archived branches (``removed_branches``),
  which date each task's start. They are read on every request (cheap, local)
  and joined to PRs by the PR identity the CLI recorded for the task (node id,
  else repo and number); only a task that never recorded its PR falls back to
  ``(repo, head branch)`` and the times, and is flagged as such.

The ``gh`` call, the stack DB read, the remote lookup and the clocks are
injected, so tests drive it with canned payloads.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from toolbelt.git.stack.store import (
    RemovedBranch,
    RepoRecord,
    StackStore,
    TrackedBranchFacts,
    default_db_path,
)
from toolbelt.ui import aggregate
from toolbelt.ui import api_models as api
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.pr_fetch import timeline_items
from toolbelt.ui.untracked_prs import (
    RemoteLookup,
    repo_slug,
    run_search_graphql,
)

DEFAULT_TTL_SECONDS = 15 * 60.0
PAGE_SIZE = 25
#: Pages per search; more PRs than this many pages hold are flagged as
#: truncated.
MAX_PAGES = 8

_TIMELINE_TYPES = (
    "READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, REVIEW_REQUESTED_EVENT, "
    "REVIEW_REQUEST_REMOVED_EVENT, PULL_REQUEST_REVIEW, PULL_REQUEST_COMMIT, "
    "HEAD_REF_FORCE_PUSHED_EVENT, MERGED_EVENT, CLOSED_EVENT, REOPENED_EVENT, "
    "BASE_REF_CHANGED_EVENT"
)
_REVIEWER = (
    "requestedReviewer { __typename ... on User { login } "
    "... on Team { name } ... on Mannequin { login } }"
)

PR_QUERY = f"""
query($search: String!, $after: String) {{
  search(query: $search, type: ISSUE, first: {PAGE_SIZE}, after: $after) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      ... on PullRequest {{
        id number title state isDraft createdAt mergedAt closedAt
        headRefName baseRefName reviewDecision
        author {{ login }}
        repository {{ nameWithOwner isArchived }}
        reviewRequests {{ totalCount }}
        timelineItems(first: 100, itemTypes: [{_TIMELINE_TYPES}]) {{
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
              checkSuites(first: 10) {{ nodes {{ status updatedAt }} }}
            }} }}
            ... on HeadRefForcePushedEvent {{
              createdAt actor {{ login }}
              beforeCommit {{ oid statusCheckRollup {{ state }} }}
              afterCommit {{ oid }}
            }}
            ... on MergedEvent {{ createdAt actor {{ login }} }}
            ... on ClosedEvent {{ createdAt actor {{ login }} }}
            ... on ReopenedEvent {{ createdAt actor {{ login }} }}
            ... on BaseRefChangedEvent {{ createdAt previousRefName currentRefName }}
          }}
        }}
      }}
    }}
  }}
}}
"""

#: (search string, cursor of the page after the previous one) -> the page's
#: GraphQL response (``{"data": ...}``).
PageRunner = Callable[[str, str | None], Awaitable[dict]]


async def gh_page(search: str, after: str | None) -> dict:
    variables = {"after": after} if after else {}
    return await run_search_graphql(PR_QUERY, search, **variables)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _review_now(node: dict) -> str:
    decision = node.get("reviewDecision") or ""
    if decision == "APPROVED":
        return "approved"
    if decision == "CHANGES_REQUESTED":
        return "reviewed"
    if (node.get("reviewRequests") or {}).get("totalCount"):
        return "requested"
    return "none"


def _ci_done(entries: list[dict]) -> dict[str, datetime]:
    """When each pushed commit's check suites completed (only those whose
    suites all had)."""
    done: dict[str, datetime] = {}
    for entry in entries:
        commit = entry.get("commit")
        if entry.get("__typename") != "PullRequestCommit" or not commit:
            continue
        suites = (commit.get("checkSuites") or {}).get("nodes") or []
        if suites and all(suite["status"] == "COMPLETED" for suite in suites):
            done[commit["oid"]] = max(_time(suite["updatedAt"]) for suite in suites)
    return done


def parse_pr(node: dict) -> aggregate.PrRecord:
    """One PR node of a ``PR_QUERY`` page."""
    entries = node["timelineItems"]["nodes"]
    facts = PrFacts(
        number=node["number"],
        title=node["title"],
        author=(node.get("author") or {}).get("login") or "",
        base_ref=node["baseRefName"],
        head_oid="",
        state=node["state"],
        is_draft=node["isDraft"],
        created_at=_time(node["createdAt"]),
        merged_at=_time(node["mergedAt"]) if node.get("mergedAt") else None,
        closed_at=_time(node["closedAt"]) if node.get("closedAt") else None,
        requested_reviewers=(),
        commit_rollups=(),
        items=timeline_items(entries),
    )
    changes = sorted(
        (_time(e["createdAt"]), e["previousRefName"], e["currentRefName"])
        for e in entries
        if e.get("__typename") == "BaseRefChangedEvent"
    )
    return aggregate.PrRecord(
        repo=node["repository"]["nameWithOwner"].lower(),
        head_branch=node["headRefName"],
        facts=facts,
        review_now=_review_now(node),
        base_changes=tuple(changes),
        ci_done=_ci_done(entries),
        node_id=node.get("id") or "",
    )


def parse_page(payload: dict) -> tuple[list[aggregate.PrRecord], bool, str | None, int]:
    """``(PRs, has_next_page, end_cursor, hidden)`` of one page. Nodes that
    are not PRs (empty) and PRs of archived repos are skipped."""
    search = payload["data"]["search"]
    records = [
        parse_pr(node)
        for node in search["nodes"]
        if node and not node["repository"]["isArchived"]
    ]
    page = search.get("pageInfo") or {}
    return (
        records,
        bool(page.get("hasNextPage")),
        page.get("endCursor"),
        len(payload.get("errors") or []),
    )


@dataclass(frozen=True)
class StoredHistory:
    """The stack DB's side: where each repo is and the tracked and archived
    branches."""

    repos: list[RepoRecord]
    removed: list[RemovedBranch]
    tracked: list[TrackedBranchFacts]
    since: datetime


def read_history() -> StoredHistory:
    store = StackStore(default_db_path())
    try:
        return StoredHistory(
            repos=store.repos(),
            removed=store.removed_branches(),
            tracked=store.tracked_branch_facts(),
            since=store.archive_since(),
        )
    finally:
        store.close()


async def task_facts(
    history: StoredHistory,
    remote_of: RemoteLookup,
    finished: Callable[[int, str], bool],
) -> list[aggregate.TaskFact]:
    """The stored branches as ``TaskFact``s keyed by ``owner/name`` (a repo
    whose remote cannot be resolved cannot be matched to PRs)."""
    slugs: dict[int, str | None] = {}
    for repo in history.repos:
        slugs[repo.id] = repo_slug(repo.remote_url) or repo_slug(
            await remote_of(repo.path)
        )
    facts = [
        aggregate.TaskFact(
            repo=slugs.get(row.repo_id),
            branch=row.name,
            started_at=row.started_at,
            finished=finished(row.repo_id, row.name),
            pr_node_id=row.pr.node_id if row.pr else None,
            pr_number=row.pr.number if row.pr else None,
        )
        for row in history.tracked
    ]
    facts += [
        aggregate.TaskFact(
            repo=slugs.get(row.repo_id),
            branch=row.name,
            started_at=row.started_at,
            removed_at=row.removed_at,
            landed=row.landed,
            pr_node_id=row.pr.node_id if row.pr else None,
            pr_number=row.pr.number if row.pr else None,
        )
        for row in history.removed
    ]
    return facts


@dataclass(frozen=True)
class _Fetched:
    weeks: int
    at: datetime
    taken: float
    prs: list[aggregate.PrRecord]
    truncated: bool
    hidden: int


class AggregateService:
    def __init__(
        self,
        *,
        search: PageRunner,
        history: Callable[[], StoredHistory],
        remote_of: RemoteLookup,
        finished: Callable[[int, str], bool],
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime],
        ttl: float = DEFAULT_TTL_SECONDS,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._search = search
        self._history = history
        self._remote_of = remote_of
        self._finished = finished
        self._clock = clock
        self._wall_clock = wall_clock
        self._ttl = ttl
        self._max_pages = max_pages
        self._cached: _Fetched | None = None
        self._lock = asyncio.Lock()

    async def _pages(self, query: str) -> tuple[list[aggregate.PrRecord], bool, int]:
        records: list[aggregate.PrRecord] = []
        hidden = 0
        cursor: str | None = None
        for page in range(self._max_pages):
            try:
                payload = await self._search(query, cursor)
            except Exception:
                if page == 0:
                    raise
                return records, True, hidden  # keep what was fetched
            found, more, cursor, errors = parse_page(payload)
            records += found
            hidden = max(hidden, errors)
            if not more or cursor is None:
                return records, False, hidden
        return records, True, hidden

    async def _fetch(self, weeks: int) -> _Fetched:
        now = self._wall_clock()
        start = aggregate.window_start(now, weeks) - timedelta(days=1)
        updated = (
            f"is:pr author:@me archived:false updated:>={start:%Y-%m-%d} "
            "sort:updated-desc"
        )
        open_now = "is:pr is:open author:@me archived:false sort:updated-desc"
        by_id: dict[tuple[str, int], aggregate.PrRecord] = {}
        truncated = False
        hidden = 0
        for query in (updated, open_now):
            records, cut, errors = await self._pages(query)
            truncated = truncated or cut
            hidden = max(hidden, errors)
            for record in records:
                by_id[(record.repo, record.facts.number)] = record
        return _Fetched(
            weeks=weeks,
            at=now,
            taken=self._clock(),
            prs=list(by_id.values()),
            truncated=truncated,
            hidden=hidden,
        )

    async def get(self, weeks: int) -> api.Aggregate:
        async with self._lock:
            cached = self._cached
            if (
                cached is None
                or cached.weeks < weeks
                or self._clock() - cached.taken >= self._ttl
            ):
                cached = self._cached = await self._fetch(weeks)
        history = self._history()
        facts = await task_facts(history, self._remote_of, self._finished)
        return aggregate.compute(
            cached.prs,
            facts,
            now=self._wall_clock(),
            weeks=weeks,
            archive_since=history.since,
            fetched_at=cached.at,
            truncated=cached.truncated,
            hidden=cached.hidden,
        )
