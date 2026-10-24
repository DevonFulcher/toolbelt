"""Where the aggregate view's inputs come from, and where they are kept.

``GET /api/aggregate`` needs two kinds of history:

* **GitHub**: the user's PRs that were updated in the window (every state)
  plus every PR still open (a quiet open PR is in flight for the whole window
  but was not "updated"), each with the timeline items the card timeline
  uses. Paginated GraphQL searches (``author:@me``, archived repos left out).
  Each PR is stored once in the stack DB (``aggregate_prs``), whichever window
  asks for it: the windows are cut from that one copy when the view is
  computed. ``aggregate_sync`` says how far back PRs have been fetched. A
  restart reads the DB and asks GitHub only what changed since the newest PR
  held; a wider window than any asked for before fetches only the older part.
  Every open PR is re-fetched every ``OPEN_SWEEP_INTERVAL`` (to notice one that
  disappeared), the rest only when GitHub says it was updated. A page that
  fails after the first, or an organization the token is not authorized for,
  yields a partial result that is flagged, as the untracked-PR callout does.
  The in-memory copy is a cache of the DB.
* **The stack DB**: tracked and archived branches (``removed_branches``),
  which date each task's start. They are read on every request (cheap, local)
  and joined to PRs by the PR identity the CLI recorded for the task (node id,
  else repo and number); only a task that never recorded its PR falls back to
  ``(repo, head branch)`` and the times, and is flagged as such.

The ``gh`` call, the stack DB read, the remote lookup and the clocks are
injected, so tests drive it with canned payloads.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from toolbelt.git.stack.store import (
    AgentTurn,
    AggregatePrRow,
    AggregateSync,
    RemovedBranch,
    RepoRecord,
    StackStore,
    TrackedBranchFacts,
    default_db_path,
)
from toolbelt.logger import logger
from toolbelt.ui import aggregate
from toolbelt.ui import api_models as api
from toolbelt.ui.github_requests import GithubRequests, GithubUnavailable
from toolbelt.ui.lane_history import build_lanes
from toolbelt.ui.pr_activity import (
    PrFacts,
    facts_from_dict,
    facts_to_dict,
    pending_reviewers,
)
from toolbelt.ui.pr_fetch import timeline_items
from toolbelt.ui.repo_visibility import visible_repos
from toolbelt.ui.untracked_prs import (
    RemoteLookup,
    repo_slug,
)

#: The version of a stored PR's JSON (``record_to_json``): rows of another
#: version are dropped and fetched again.
RECORD_SCHEMA = 1
#: How often every open PR is fetched again, not only those updated.
OPEN_SWEEP_INTERVAL = timedelta(hours=6)
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
query($search: String!, $after: String, $first: Int!) {{
  search(query: $search, type: ISSUE, first: $first, after: $after) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      ... on PullRequest {{
        id number url title state isDraft createdAt updatedAt mergedAt closedAt
        headRefName baseRefName reviewDecision additions deletions
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


#: How many PRs a request asks for, tried in turn: each PR carries up to a
#: hundred timeline events, so a page of PRs with long histories (a PR merged
#: with its parent on every sync has dozens of commits) can be too much for
#: GitHub to answer in time, and it says 504. A smaller size is kept once it
#: has worked.
REQUEST_SIZES = (PAGE_SIZE, 10, 5, 2, 1)


def page_runner(requests: GithubRequests) -> PageRunner:
    """The history search over ``requests``. A page of ``PAGE_SIZE`` PRs is
    fetched in as many requests as GitHub will answer (see ``REQUEST_SIZES``)."""
    size = 0  # index into REQUEST_SIZES

    async def run(search: str, after: str | None) -> dict:
        nonlocal size
        nodes: list = []
        errors: list = []
        page: dict = {}
        cursor = after
        remaining = PAGE_SIZE
        while remaining > 0:
            first = min(REQUEST_SIZES[size], remaining)
            variables: dict[str, str | int] = {"search": search, "first": first}
            if cursor:
                variables["after"] = cursor
            try:
                payload = await requests.graphql(
                    PR_QUERY, variables, caller="aggregate", partial="search"
                )
            except GithubUnavailable:
                if size == len(REQUEST_SIZES) - 1:
                    raise
                size += 1
                continue
            found = payload["data"]["search"]
            nodes += found["nodes"]
            errors += payload.get("errors") or []
            page = found.get("pageInfo") or {}
            remaining -= first
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
        return {
            "data": {"search": {"nodes": nodes, "pageInfo": page}},
            "errors": errors,
        }

    return run


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


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
    items = timeline_items(entries)
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
        requested_reviewers=pending_reviewers(items) if node["state"] == "OPEN" else (),
        commit_rollups=(),
        items=items,
        additions=node.get("additions") or 0,
        deletions=node.get("deletions") or 0,
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
    #: What each lane recorded for each task (``lane_history``), by task id.
    lanes: dict[str, list[api.TimelineLane]] = field(default_factory=dict)
    #: The agent turns of the last year (``agent_activity``).
    turns: list[AgentTurn] = field(default_factory=list)


def read_history() -> StoredHistory:
    store = StackStore(default_db_path())
    try:
        repos = visible_repos(store.repos())
        ids = {repo.id for repo in repos}
        slugs = {repo.id: repo_slug(repo.remote_url) for repo in repos}
        lanes = build_lanes(store, datetime.now(UTC), slugs.get)
        return StoredHistory(
            repos=repos,
            removed=[row for row in store.removed_branches() if row.repo_id in ids],
            tracked=[row for row in store.tracked_branch_facts() if row.repo_id in ids],
            since=store.archive_since(),
            lanes=lanes,
            turns=[
                turn
                for turn in store.agent_turns(
                    since=datetime.now(UTC) - timedelta(weeks=52)
                )
                if turn.repo_id is None or turn.repo_id in ids
            ],
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
            lanes=tuple(history.lanes.get(row.task_id, ())),
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
            lanes=tuple(history.lanes.get(row.task_id, ())),
        )
        for row in history.removed
    ]
    return facts


@dataclass
class _Cache:
    """The stored PRs, by ``(repo, number)``, kept in memory (the DB is the
    copy that lasts)."""

    records: dict[tuple[str, int], aggregate.PrRecord]
    #: PRs updated on or after this have been fetched.
    covered_from: datetime
    #: The first fetch was cut short, or hid some organizations: older PRs
    #: missing from it are never fetched by the incremental ones.
    truncated: bool
    hidden: int
    fetched_at: datetime
    refreshed: int
    #: When every open PR was last fetched again (``None``: never).
    open_swept_at: datetime | None


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_or_none(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def record_to_json(record: aggregate.PrRecord) -> str:
    """A PR as the JSON ``record_from_json`` rebuilds it from."""
    facts = record.facts
    return json.dumps(
        {
            "repo": record.repo,
            "head_branch": record.head_branch,
            "review_now": record.review_now,
            "base_changes": [
                [when.isoformat(), previous, current]
                for when, previous, current in record.base_changes
            ],
            "node_id": record.node_id,
            "updated_at": _iso_or_none(record.updated_at),
            "url": record.url,
            "facts": facts_to_dict(facts),
        },
        separators=(",", ":"),
    )


def record_from_json(text: str) -> aggregate.PrRecord:
    data = json.loads(text)
    facts = facts_from_dict(data["facts"])
    return aggregate.PrRecord(
        repo=data["repo"],
        head_branch=data["head_branch"],
        facts=facts,
        review_now=data["review_now"],
        base_changes=tuple(
            (datetime.fromisoformat(when), previous, current)
            for when, previous, current in data["base_changes"]
        ),
        node_id=data["node_id"],
        updated_at=_parse_or_none(data["updated_at"]),
        url=data["url"],
    )


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
    """Serves the aggregate view from the PRs stored in the stack DB.

    The first request on an empty DB pages through the whole window. After
    that a restart loads what is stored and a refresh asks GitHub only for (a)
    the PRs updated since the newest ``updatedAt`` held (less a small margin)
    and (b), every ``OPEN_SWEEP_INTERVAL``, all the PRs open now. A window
    wider than any asked for before fetches only the part older than what is
    stored. Fetched PRs are merged by key and written back, so each PR is
    stored once for every window.
    """

    def __init__(
        self,
        *,
        search: PageRunner,
        history: Callable[[], StoredHistory],
        remote_of: RemoteLookup,
        finished: Callable[[int, str], bool],
        wall_clock: Callable[[], datetime],
        db_path: Callable[[], Path] = default_db_path,
        max_pages: int = MAX_PAGES,
        margin: timedelta = UPDATED_MARGIN,
        estimate_accuracy: Callable[[], api.EstimateAccuracy | None] = lambda: None,
    ) -> None:
        self._estimate_accuracy = estimate_accuracy
        self._search = search
        self._history = history
        self._remote_of = remote_of
        self._finished = finished
        self._wall_clock = wall_clock
        self._db_path = db_path
        self._max_pages = max_pages
        self._margin = margin
        self._cache: _Cache | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> _Cache | None:
        """The stored history (blocking), or ``None`` when there is none or
        it was written by another version."""
        store = StackStore(self._db_path())
        try:
            sync = store.aggregate_sync()
            if sync is None:
                return None
            if sync.schema != RECORD_SCHEMA:
                store.clear_aggregate()
                return None
            records = {}
            for row in store.aggregate_prs():
                record = record_from_json(row.record)
                records[_key(record)] = record
        finally:
            store.close()
        return _Cache(
            records=records,
            covered_from=sync.covered_from,
            truncated=sync.truncated,
            hidden=sync.hidden,
            fetched_at=sync.fetched_at,
            refreshed=sync.refreshed,
            open_swept_at=sync.open_swept_at,
        )

    def _save(
        self,
        cache: _Cache,
        changed: list[aggregate.PrRecord],
        removed: list[tuple[str, int]],
    ) -> None:
        store = StackStore(self._db_path())
        try:
            store.save_aggregate(
                changed=[
                    AggregatePrRow(
                        repo=record.repo,
                        number=record.facts.number,
                        updated_at=record.updated_at,
                        record=record_to_json(record),
                    )
                    for record in changed
                ],
                removed=removed,
                sync=AggregateSync(
                    covered_from=cache.covered_from,
                    fetched_at=cache.fetched_at,
                    refreshed=cache.refreshed,
                    truncated=cache.truncated,
                    hidden=cache.hidden,
                    open_swept_at=cache.open_swept_at,
                    schema=RECORD_SCHEMA,
                ),
            )
        finally:
            store.close()

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

    async def _first_fetch(
        self, start: datetime, now: datetime
    ) -> tuple[_Cache, list[aggregate.PrRecord]]:
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
        cache = _Cache(
            records=by_id,
            covered_from=start,
            truncated=truncated,
            hidden=hidden,
            fetched_at=now,
            refreshed=len(by_id),
            open_swept_at=now,
        )
        return cache, list(by_id.values())

    async def _fetch_older(
        self, cache: _Cache, start: datetime, now: datetime
    ) -> list[aggregate.PrRecord]:
        """Fetches the PRs updated between ``start`` and what is stored, for a
        window wider than any before."""
        found, cut, hidden = await self._pages(
            "is:pr author:@me archived:false "
            f"updated:{start:%Y-%m-%d}..{cache.covered_from:%Y-%m-%d} "
            "sort:updated-desc"
        )
        cache.records.update({_key(record): record for record in found})
        cache.covered_from = start
        cache.truncated = cache.truncated or cut
        cache.hidden = max(cache.hidden, hidden)
        cache.fetched_at = now
        cache.refreshed = len(found)
        return found

    async def _refresh(
        self, cache: _Cache, since: datetime, now: datetime
    ) -> tuple[list[aggregate.PrRecord], list[tuple[str, int]]]:
        """Fetches the PRs updated since ``since`` (and every open one, when
        that has not been done for ``OPEN_SWEEP_INTERVAL``) and merges them
        into ``cache``. Returns what was fetched and what has gone."""
        changed, cut, hidden = await self._pages(
            "is:pr author:@me archived:false "
            f"updated:>={since:%Y-%m-%dT%H:%M:%SZ} sort:updated-desc"
        )
        sweep = (
            cache.open_swept_at is None
            or now - cache.open_swept_at >= OPEN_SWEEP_INTERVAL
        )
        open_now: list[aggregate.PrRecord] = []
        open_cut = False
        if sweep:
            open_now, open_cut, open_hidden = await self._pages(OPEN_NOW)
            hidden = max(hidden, open_hidden)
        fresh = {_key(r): r for r in [*changed, *open_now]}
        cache.records.update(fresh)
        removed: list[tuple[str, int]] = []
        if sweep and not open_cut:
            cache.open_swept_at = now
            # An open PR that is neither open now nor was updated has gone
            # (deleted, or transferred out of reach): do not keep showing it.
            for key, record in list(cache.records.items()):
                if record.facts.state == "OPEN" and key not in fresh:
                    del cache.records[key]
                    removed.append(key)
        cache.truncated = cache.truncated or cut or open_cut
        cache.hidden = max(cache.hidden, hidden)
        cache.fetched_at = now
        cache.refreshed = len(fresh)
        return list(fresh.values()), removed

    async def run(self, interval: float = 900.0) -> None:
        """Warm 12 weeks, then refresh the widest window requested."""
        while True:
            try:
                await self.get(aggregate.DEFAULT_WEEKS)
            except Exception:  # noqa: BLE001
                logger.exception("Could not refresh aggregate history")
            await asyncio.sleep(interval)

    async def get(
        self, weeks: int, repo: str | None = None, *, refresh: bool = True
    ) -> api.Aggregate:
        """The aggregate of the last ``weeks`` weeks; ``repo`` (a stack DB
        repo id or a repo's name) narrows it to that repo's PRs and tasks.
        Raises ``LookupError`` for a repo that is not known."""
        history = self._history()
        chosen = _find_repo(history.repos, repo) if repo else None
        start = aggregate.window_start(self._wall_clock(), weeks) - timedelta(days=1)
        ready = self._cache
        if ready is None:
            ready = await asyncio.to_thread(self._load)
            if self._cache is None:
                self._cache = ready
        if not refresh and ready is not None and ready.covered_from <= start:
            # A background GitHub request may be in flight. Its cache update
            # is synchronous, so this snapshot never waits on network I/O.
            cache = ready
        else:
            async with self._lock:
                now = self._wall_clock()
                held = self._cache
                if held is None:
                    held = self._cache = await asyncio.to_thread(self._load)
                newest = (
                    max(
                        (
                            r.updated_at
                            for r in held.records.values()
                            if r.updated_at is not None
                        ),
                        default=None,
                    )
                    if held is not None
                    else None
                )
                changed: list[aggregate.PrRecord]
                removed: list[tuple[str, int]] = []
                if held is None or (refresh and newest is None):
                    cache, changed = await self._first_fetch(start, now)
                elif start < held.covered_from:
                    cache = held
                    changed = await self._fetch_older(cache, start, now)
                elif refresh and newest is not None:
                    cache = held
                    changed, removed = await self._refresh(
                        cache, newest - self._margin, now
                    )
                else:
                    cache = held
                    changed = []
                if changed or removed:
                    await asyncio.to_thread(self._save, cache, changed, removed)
                self._cache = cache
        prs = list(cache.records.values())
        facts = await task_facts(history, self._remote_of, self._finished)
        if chosen is not None:
            slug = repo_slug(chosen.remote_url) or repo_slug(
                await self._remote_of(chosen.path)
            )
            # A repo whose remote cannot be resolved cannot be matched to PRs.
            prs = [p for p in prs if slug is not None and p.repo == slug]
            facts = [f for f in facts if slug is not None and f.repo == slug]
        computed = aggregate.compute(
            prs,
            facts,
            now=self._wall_clock(),
            weeks=weeks,
            archive_since=history.since,
            fetched_at=cache.fetched_at,
            refreshed=cache.refreshed,
            truncated=cache.truncated,
            hidden=cache.hidden,
            repo=chosen.name if chosen else None,
            turns=[
                turn
                for turn in history.turns
                if chosen is None or turn.repo_id == chosen.id
            ],
        )
        accuracy = await asyncio.to_thread(self._estimate_accuracy)
        return computed.model_copy(update={"estimate": accuracy})

    async def lives(self) -> list[tuple[aggregate.Task, list[aggregate.Segment]]]:
        """Every cached PR's task and its Next over its life, as stored (no
        request to GitHub): what the finish estimate's history is built from."""
        ready = self._cache
        if ready is None:
            ready = self._cache = await asyncio.to_thread(self._load)
        if ready is None:
            return []
        history = await asyncio.to_thread(self._history)
        facts = await task_facts(history, self._remote_of, self._finished)
        now = self._wall_clock()

        def work() -> list[tuple[aggregate.Task, list[aggregate.Segment]]]:
            tasks = aggregate.match_tasks(list(ready.records.values()), facts, now)
            with_pr, resolved, _ = aggregate.pr_lives(tasks, now)
            return list(zip(with_pr, resolved, strict=True))

        return await asyncio.to_thread(work)
