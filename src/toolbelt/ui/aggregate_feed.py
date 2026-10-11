"""Where the aggregate view's inputs come from, and their cache.

``GET /api/aggregate`` needs two kinds of history:

* **GitHub**: the user's PRs that were updated in the window (every state)
  plus every PR still open (a quiet open PR is in flight for the whole window
  but was not "updated"), each with the timeline items the card timeline
  uses. Paginated GraphQL searches (``author:@me``, archived repos left out).
  The PRs are kept in memory keyed by ``(repo, number)`` with GitHub's
  ``updatedAt``. A background loop refreshes the open PRs and whatever was
  updated since the newest one held (see ``AggregateService``). A larger
  cached window serves a smaller one. A page
  that fails after the first, or an organization the token is not authorized
  for, yields a partial result that is flagged, as the untracked-PR callout
  does.
* **The stack DB**: tracked and archived branches (``removed_branches``),
  which date each task's start. They are read on every request (cheap, local)
  and joined to PRs by the PR identity the CLI recorded for the task (node id,
  else repo and number); only a task that never recorded its PR falls back to
  ``(repo, head branch)`` and the times, and is flagged as such.

The ``gh`` call, the stack DB read, the remote lookup and the clocks are
injected, so tests drive it with canned payloads.
"""

import asyncio
import logging
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

#: Subtracted from the newest ``updatedAt`` held when asking GitHub what
#: changed since (clock skew, and a search index that lags a little).
UPDATED_MARGIN = timedelta(minutes=5)
OPEN_NOW = "is:pr is:open author:@me archived:false sort:updated-desc"
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
        id number url title state isDraft createdAt updatedAt mergedAt closedAt
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
        node_id=node.get("id") or "",
        updated_at=_time(node["updatedAt"]) if node.get("updatedAt") else None,
        url=node.get("url") or "",
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


@dataclass
class _Cache:
    """The PRs fetched so far, by ``(repo, number)``, for the widest window
    asked for."""

    weeks: int
    records: dict[tuple[str, int], aggregate.PrRecord]
    #: The first (full) fetch was cut short, or hid some organizations: older
    #: PRs missing from it are never fetched by the incremental ones.
    truncated: bool
    hidden: int
    fetched_at: datetime
    refreshed: int


def _find_repo(repos: list[RepoRecord], wanted: str) -> RepoRecord:
    """The repo with this id, else this name (as the other views' ``repo``
    filter, which is the id)."""
    for repo in repos:
        if str(repo.id) == wanted:
            return repo
    for repo in repos:
        if repo.name.lower() == wanted.lower():
            return repo
    raise LookupError(f"unknown repo: {wanted}")


def _key(record: aggregate.PrRecord) -> tuple[str, int]:
    return (record.repo, record.facts.number)


class AggregateService:
    """Serves the aggregate view from PRs kept in memory.

    The first refresh (or one for a wider window than any before) pages
    through the whole window. Every later refresh asks GitHub for (a) all PRs
    that are open now and (b) those updated since the newest ``updatedAt``
    held (less a small margin), and merges them by key: so only PRs that
    changed are queried again, a PR that was closed, merged, reopened or
    commented on is replaced at each refresh.
    The cache is not persisted: the background loop warms it after a restart.
    """

    def __init__(
        self,
        *,
        search: PageRunner,
        history: Callable[[], StoredHistory],
        remote_of: RemoteLookup,
        finished: Callable[[int, str], bool],
        wall_clock: Callable[[], datetime],
        max_pages: int = MAX_PAGES,
        margin: timedelta = UPDATED_MARGIN,
    ) -> None:
        self._search = search
        self._history = history
        self._remote_of = remote_of
        self._finished = finished
        self._wall_clock = wall_clock
        self._max_pages = max_pages
        self._margin = margin
        self._cache: _Cache | None = None
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

    async def _first_fetch(self, weeks: int, now: datetime) -> tuple[_Cache, int]:
        start = aggregate.window_start(now, weeks) - timedelta(days=1)
        updated = (
            f"is:pr author:@me archived:false updated:>={start:%Y-%m-%d} "
            "sort:updated-desc"
        )
        by_id: dict[tuple[str, int], aggregate.PrRecord] = {}
        truncated = False
        hidden = 0
        for query in (updated, OPEN_NOW):
            records, cut, errors = await self._pages(query)
            truncated = truncated or cut
            hidden = max(hidden, errors)
            for record in records:
                by_id[_key(record)] = record
        return _Cache(weeks, by_id, truncated, hidden, now, len(by_id)), len(by_id)

    async def _refresh(self, cache: _Cache, since: datetime) -> tuple[int, bool, int]:
        """Re-fetches the open PRs and those updated since ``since``, merging
        them into ``cache``. Returns ``(PRs refreshed, cut short, hidden)``."""
        open_now, open_cut, open_hidden = await self._pages(OPEN_NOW)
        changed, cut, hidden = await self._pages(
            "is:pr author:@me archived:false "
            f"updated:>={since:%Y-%m-%dT%H:%M:%SZ} sort:updated-desc"
        )
        fresh = {_key(r): r for r in [*changed, *open_now]}
        cache.records.update(fresh)
        if not open_cut:
            # An open PR that is neither open now nor was updated has gone
            # (deleted, or transferred out of reach): do not keep showing it.
            for key, record in list(cache.records.items()):
                if record.facts.state == "OPEN" and key not in fresh:
                    del cache.records[key]
        return len(fresh), open_cut or cut, max(open_hidden, hidden)

    async def run(self, interval: float = 900.0) -> None:
        """Warm 12 weeks, then refresh the widest window requested."""
        while True:
            try:
                await self.get(
                    max(
                        aggregate.DEFAULT_WEEKS, self._cache.weeks if self._cache else 0
                    )
                )
            except Exception:
                logging.exception("Could not refresh aggregate history")
            await asyncio.sleep(interval)

    async def get(
        self, weeks: int, repo: str | None = None, *, refresh: bool = True
    ) -> api.Aggregate:
        """The aggregate of the last ``weeks`` weeks; ``repo`` (a stack DB
        repo id or a repo's name) narrows it to that repo's PRs and tasks.
        Raises ``LookupError`` for a repo that is not known."""
        history = self._history()
        chosen = _find_repo(history.repos, repo) if repo else None
        ready = self._cache
        if not refresh and ready is not None and ready.weeks >= weeks:
            # A background GitHub request may be in flight. Its cache update
            # is synchronous, so this snapshot never waits on network I/O.
            prs = list(ready.records.values())
            truncated, hidden = ready.truncated, ready.hidden
            fetched_at, refreshed = ready.fetched_at, ready.refreshed
        else:
            async with self._lock:
                now = self._wall_clock()
                cache = self._cache
                newest = (
                    max(
                        (
                            r.updated_at
                            for r in cache.records.values()
                            if r.updated_at is not None
                        ),
                        default=None,
                    )
                    if cache is not None
                    else None
                )
                if cache is None or cache.weeks < weeks or (refresh and newest is None):
                    cache, refreshed = await self._first_fetch(weeks, now)
                    self._cache = cache
                elif refresh and newest is not None:
                    refreshed, cut, errors = await self._refresh(
                        cache, newest - self._margin
                    )
                    cache.truncated = cache.truncated or cut
                    cache.hidden = max(cache.hidden, errors)
                    cache.fetched_at = now
                    cache.refreshed = refreshed
                truncated, hidden = cache.truncated, cache.hidden
                prs = list(cache.records.values())
                fetched_at, refreshed = cache.fetched_at, cache.refreshed
        facts = await task_facts(history, self._remote_of, self._finished)
        if chosen is not None:
            slug = repo_slug(chosen.remote_url) or repo_slug(
                await self._remote_of(chosen.path)
            )
            # A repo whose remote cannot be resolved cannot be matched to PRs.
            prs = [p for p in prs if slug is not None and p.repo == slug]
            facts = [f for f in facts if slug is not None and f.repo == slug]
        return aggregate.compute(
            prs,
            facts,
            now=self._wall_clock(),
            weeks=weeks,
            archive_since=history.since,
            fetched_at=fetched_at,
            refreshed=refreshed,
            truncated=truncated,
            hidden=hidden,
            repo=chosen.name if chosen else None,
        )
