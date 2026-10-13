"""The `tt ui` web app: every known repo's stacks, live, read-only.

The server is a JSON API plus the built web client (``web/``, served from
``static/dist``):

* ``GET /api/snapshot``: every tracked branch of every repo (stack structure,
  links, live status, the state catalog), straight from the stack DB plus the
  in-memory caches (no network on the request path); see ``snapshot.py`` and
  ``api_models.py``;
* ``GET /api/aggregate?weeks=N[&repo=<id or name>]``: how work has flowed over the last N weeks
  (throughput, lead times, status mix), computed from cached history; see
  ``aggregate.py`` and ``aggregate_feed.py``;
* ``GET /api/events``: a Server-Sent Events stream of typed events that keep a
  snapshot current:

  - ``hello``: sent on connect with the current event version, so a browser
    that reconnects knows whether it missed anything;
  - ``status``: one branch's new live status (also sent for the branches
    stacked on it, whose next action depends on it);
  - ``task``: a branch's task title/status was (re)fetched;
  - ``structure``: the stack DB was written (new branch, re-parenting, new
    links), so the page re-fetches the snapshot;
  - ``claims``: some branches' claim holders changed (an agent started or
    stopped working on them, or a claim expired); carries each affected
    branch's claim and re-derived status, including the branches stacked on
    it (see ``claims_feed.py``, ``task_claims``);
  - ``refreshed``: a polling pass finished (drives "updated Ns ago");
  - ``untracked``: the list of the user's open PRs that are not tracked
    branches changed (see ``untracked_prs.py``).

A background ``StatusRefresher`` (``live_status.py``) keeps the status cache
current by polling GitHub (one GraphQL call per branch, see ``pr_fetch.py``)
while any browser is connected; a ``TaskFeed`` does the same for tasks and
an ``UntrackedPrFeed`` (slowly) for the PRs toolbelt does not track.
``AggregateService`` warms 12 weeks, then refreshes the widest requested
window every fifteen minutes.

The status lookup (``fetch``), the task tracker and the clocks are
``create_app`` arguments, so tests can inject them.
"""

import asyncio
import contextlib
import functools
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack.store import Parents, StackStore, default_db_path
from toolbelt.tasks.config import configured_tracker
from toolbelt.tasks.tracker import TaskTracker
from toolbelt.git.stack.status import PrState
from toolbelt.ui import aggregate as aggregate_view
from toolbelt.ui import api_models as api
from toolbelt.ui import branch_links
from toolbelt.ui.aggregate_feed import (
    AggregateService,
    PageRunner,
    StoredHistory,
    gh_page,
    read_history,
)
from toolbelt.git.stack.store import state_dir
from toolbelt.task_claims.claim_file import CLAIMS_DIRNAME
from toolbelt.task_claims.resolution import TrackedTask, git_worktrees, tracked_tasks
from toolbelt.ui.claims_feed import CLAIMS_TICK_SECONDS, BranchKey, ClaimsFeed
from toolbelt.ui.client_build import CLIENT_DIST
from toolbelt.ui.live_status import (
    DEFAULT_CONCURRENCY,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_SETTLED_INTERVAL_SECONDS,
    DEFAULT_TICK_SECONDS,
    EventHub,
    Fetched,
    StatusFetcher,
    StatusRefresher,
    StructureWatcher,
    TrackedBranch,
)
from toolbelt.ui.pr_fetch import PrSnapshot, fetch_snapshot
from toolbelt.ui.snapshot import (
    branch_claim,
    build_snapshot,
    descendants,
    live_status,
    parent_context,
    row_id,
)
from toolbelt.ui.task_cache import TaskInfoCache
from toolbelt.ui.task_feed import LinkedTask, TaskFeed
from toolbelt.ui.untracked_prs import (
    DEFAULT_INTERVAL_SECONDS as DEFAULT_UNTRACKED_INTERVAL_SECONDS,
)
from toolbelt.ui.untracked_prs import (
    RemoteLookup,
    SearchRunner,
    TrackedRepo,
    UntrackedPrFeed,
    gh_search,
    origin_remote,
)


# Sent when no event has gone out for this long, so intermediaries keep the
# stream open and a dead client is noticed.
_KEEPALIVE_SECONDS = 15.0


async def fetch_status(branch: str, root: Path) -> PrSnapshot:
    return await fetch_snapshot(branch, root)


@functools.cache
def _tracker_from_config(config_file: Path) -> TaskTracker | None:
    # Built once per config path so the tracker's HTTP client and resolved
    # token are reused across requests.
    return configured_tracker(config_file)


def task_tracker() -> TaskTracker | None:
    return _tracker_from_config(default_config_path())


def _all_parents(repo_id: int) -> Parents:
    store = StackStore(default_db_path())
    try:
        return store.all_parents(repo_id)
    finally:
        store.close()


def _tracked_branches() -> list[TrackedBranch]:
    """Every tracked branch whose checkout exists: what is worth polling."""
    store = StackStore(default_db_path())
    try:
        return [
            TrackedBranch(repo_id=repo.id, branch=name, path=repo.path)
            for repo in store.repos()
            if repo.path.is_dir()
            for name in store.all_parents(repo.id)
        ]
    finally:
        store.close()


def _tracked_repos() -> list[TrackedRepo]:
    """Every registered repo with its tracked branches (what the callout of
    untracked PRs excludes)."""
    store = StackStore(default_db_path())
    try:
        return [
            TrackedRepo(
                path=repo.path,
                remote_url=repo.remote_url,
                branches=frozenset(store.all_parents(repo.id)),
            )
            for repo in store.repos()
        ]
    finally:
        store.close()


def _linked_tasks(tracker: TaskTracker | None) -> list[LinkedTask]:
    """Every tracked branch's stored task ref that the configured tracker
    owns: the only refs that ever reach a tracker."""
    if tracker is None:
        return []
    store = StackStore(default_db_path())
    try:
        tasks = []
        for repo in store.repos():
            for name in store.all_parents(repo.id):
                links = store.get_links(repo.id, name)
                if links is None or not links.task:
                    continue
                owner = branch_links.owning_tracker(links.task_tracker, tracker)
                if owner is not None:
                    tasks.append(LinkedTask(repo.id, name, links.task, owner))
        return tasks
    finally:
        store.close()


def _claims_dir() -> Path:
    return state_dir() / CLAIMS_DIRNAME


def _claim_tasks() -> list[TrackedTask]:
    store = StackStore(default_db_path())
    try:
        return tracked_tasks(store, git_worktrees)
    finally:
        store.close()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps({'type': event, **data})}\n\n"


def create_app(
    cwd_root: Path | None = None,
    *,
    fetch: StatusFetcher = fetch_status,
    tracker: Callable[[], TaskTracker | None] = task_tracker,
    task_ttl_seconds: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    refresh_interval: float = DEFAULT_INTERVAL_SECONDS,
    settled_refresh_interval: float = DEFAULT_SETTLED_INTERVAL_SECONDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    tick: float = DEFAULT_TICK_SECONDS,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    db_version: Callable[[], int] | None = None,
    keepalive_seconds: float = _KEEPALIVE_SECONDS,
    client_dist: Path = CLIENT_DIST,
    search_open_prs: SearchRunner = gh_search,
    remote_of: RemoteLookup = origin_remote,
    untracked_interval: float = DEFAULT_UNTRACKED_INTERVAL_SECONDS,
    search_history: PageRunner = gh_page,
    stored_history: Callable[[], StoredHistory] = read_history,
    claims_dir: Callable[[], Path] = _claims_dir,
    claim_tasks: Callable[[], list[TrackedTask]] = _claim_tasks,
    claims_tick: float | None = None,
) -> FastAPI:
    """``cwd_root``: the repo `tt ui` was launched from; its checked-out
    branch is marked current, as `tt g tree` does. ``fetch`` looks up a
    branch's PR status; ``tracker`` returns the configured task tracker;
    ``db_version`` reports when the stack DB changes (default: ``PRAGMA
    data_version`` on a connection held while serving). ``clock`` times the
    polling and caches; ``wall_clock`` (UTC) dates the ages shown. Fetched
    task info is cached for ``task_ttl_seconds``. Polling carries on for
    ``grace_seconds`` after the last browser disconnects. The background
    pollers start with the app's lifespan, so an app that is not served (a
    bare ``TestClient``) never polls; tests drive ``app.state.refresher``,
    ``app.state.task_feed`` and ``app.state.structure_watcher`` directly.
    ``client_dist`` is the built web client. ``search_open_prs`` lists the
    user's open PRs (one GitHub call per ``untracked_interval``) and
    ``remote_of`` looks up a checkout's origin: see ``untracked_prs.py``.
    ``search_history`` pages through the user's PRs for the aggregate view
    (kept fresh incrementally, see ``AggregateService``) and
    ``stored_history`` reads the
    stack DB's tracked and archived branches: see ``aggregate_feed.py``.
    ``claims_dir`` is where agents' claim files are (re-read every
    ``claims_tick`` seconds while watched; default: ``tick``) and
    ``claim_tasks`` lists the tracked branches a claim can resolve to: see
    ``claims_feed.py``."""
    task_cache = TaskInfoCache(ttl_seconds=task_ttl_seconds, clock=clock)
    hub = EventHub(clock=clock, grace=grace_seconds)

    def branch_status(repo_id: int, parents: Parents, name: str, now: datetime):
        return live_status(
            refresher.get(repo_id, name),
            parent_context(repo_id, parents, name, refresher, claims),
            now,
            claims.working(repo_id, name),
        )

    def publish_claims(changed: set[BranchKey]) -> None:
        """One event for every changed branch and the branches stacked on
        them: whether a branch is claimed decides its next action, and a
        child's follows its parent's."""
        now = wall_clock()
        affected: dict[BranchKey, Parents] = {}
        for repo_id, branch in sorted(changed):
            parents = _all_parents(repo_id)
            for name in descendants(parents, branch):
                affected[(repo_id, name)] = parents
        if not affected:
            return
        hub.publish(
            "claims",
            {
                "branches": [
                    {
                        "branch_id": row_id(repo_id, name),
                        "claim": branch_claim(
                            claims.holders(repo_id, name), now
                        ).model_dump(mode="json"),
                        "status": branch_status(repo_id, parents, name, now).model_dump(
                            mode="json"
                        ),
                    }
                    for (repo_id, name), parents in affected.items()
                ]
            },
        )

    def publish_status(branch: TrackedBranch, fetched: Fetched) -> None:
        # The cache already holds ``fetched``, so parent contexts see it.
        parents = _all_parents(branch.repo_id)
        now = wall_clock()
        for name in descendants(parents, branch.branch):
            status = branch_status(branch.repo_id, parents, name, now)
            hub.publish(
                "status",
                {
                    "branch_id": row_id(branch.repo_id, name),
                    "status": status.model_dump(mode="json"),
                },
            )

    claims = ClaimsFeed(
        directory=claims_dir,
        tasks=claim_tasks,
        hub=hub,
        on_change=publish_claims,
        wall_clock=wall_clock,
        tick=min(tick, CLAIMS_TICK_SECONDS) if claims_tick is None else claims_tick,
    )

    refresher = StatusRefresher(
        branches=_tracked_branches,
        fetch=fetch,
        hub=hub,
        on_change=publish_status,
        clock=clock,
        interval=refresh_interval,
        settled_interval=settled_refresh_interval,
        concurrency=concurrency,
        tick=tick,
    )
    task_feed = TaskFeed(
        tasks=lambda: _linked_tasks(tracker()),
        cache=task_cache,
        hub=hub,
        tick=tick,
    )

    untracked_feed = UntrackedPrFeed(
        repos=_tracked_repos,
        search=search_open_prs,
        hub=hub,
        remote_of=remote_of,
        clock=clock,
        interval=untracked_interval,
        tick=tick,
    )

    def pr_finished(repo_id: int, branch: str) -> bool:
        fetched = refresher.get(repo_id, branch)
        return (
            fetched is not None
            and fetched.status is not None
            and fetched.status.pr in (PrState.MERGED, PrState.CLOSED)
        )

    aggregates = AggregateService(
        search=search_history,
        history=stored_history,
        remote_of=remote_of,
        finished=pr_finished,
        wall_clock=wall_clock,
    )

    # The default change signal needs a connection held for as long as the app
    # serves (``data_version`` is per connection), opened on the event loop's
    # thread, which is where the watcher reads it.
    held_store: list[StackStore] = []

    def held_data_version() -> int:
        if not held_store:
            held_store.append(StackStore(default_db_path()))
        return held_store[0].data_version()

    watcher = StructureWatcher(
        version=db_version or held_data_version, hub=hub, tick=tick
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        watcher.check()  # baseline: only later writes are changes
        tasks = [
            asyncio.create_task(refresher.run()),
            asyncio.create_task(task_feed.run()),
            asyncio.create_task(claims.run()),
            asyncio.create_task(untracked_feed.run()),
            asyncio.create_task(watcher.run()),
            asyncio.create_task(aggregates.run()),
        ]
        try:
            yield
        finally:
            hub.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for store in held_store:
                store.close()

    app = FastAPI(
        title="toolbelt",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.hub = hub
    app.state.refresher = refresher
    app.state.task_feed = task_feed
    app.state.claims_feed = claims
    app.state.untracked_feed = untracked_feed
    app.state.structure_watcher = watcher
    app.state.aggregates = aggregates

    @app.get("/assets/{path:path}", include_in_schema=False, response_model=None)
    def asset(path: str) -> FileResponse:
        root = (client_dist / "assets").resolve()
        file = (root / path).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            raise HTTPException(status_code=404)
        # Content-hashed file names: cacheable for good.
        return FileResponse(
            file, headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    # The mark is linked from index.html; `/favicon.ico` is what browsers ask
    # for without looking at the page, so it serves the same SVG.
    @app.get("/favicon.svg", include_in_schema=False, response_model=None)
    @app.get("/favicon.ico", include_in_schema=False, response_model=None)
    def favicon() -> FileResponse:
        file = client_dist / "favicon.svg"
        if not file.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            file,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/", include_in_schema=False, response_model=None)
    def index() -> FileResponse | PlainTextResponse:
        page = client_dist / "index.html"
        if not page.is_file():
            return PlainTextResponse(
                "The web client is not built: run `pnpm install && pnpm build` "
                "in web/ (`tt ui` does this itself).",
                status_code=503,
            )
        # The page's assets are content-hashed; the shell must not be cached.
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    @app.get("/api/snapshot", response_model=api.Snapshot)
    async def snapshot() -> api.Snapshot:
        # Claims are only polled while a browser is connected, and a page
        # asks for the snapshot before its stream opens: bring them up to
        # date first (on the event loop, where the feed lives; a change is
        # published before the version is read, so it is in this snapshot).
        claims.poll()
        # Read before the branches: an event published meanwhile then makes
        # the snapshot look stale to /api/events, never the other way round.
        version = hub.version
        return build_snapshot(
            cwd_root,
            tracker=tracker(),
            refresher=refresher,
            task_feed=task_feed,
            untracked=untracked_feed.get(),
            now=wall_clock(),
            version=version,
            claims=claims,
        )

    @app.get("/api/aggregate", response_model=api.Aggregate)
    async def aggregate(
        weeks: int = Query(aggregate_view.DEFAULT_WEEKS),
        repo: str | None = Query(None),
        refresh: bool = Query(False),
    ) -> api.Aggregate:
        # Use the background-refreshed history; an early request can warm it.
        if weeks not in aggregate_view.WINDOWS:
            raise HTTPException(
                status_code=400,
                detail=f"weeks must be one of {list(aggregate_view.WINDOWS)}",
            )
        try:
            return await aggregates.get(weeks, repo or None, refresh=refresh)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            queue = hub.subscribe()
            try:
                yield _sse(
                    "hello",
                    {"v": hub.version, "age": refresher.seconds_since_refresh()},
                )
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), keepalive_seconds)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        return
                    yield _sse(*item)
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
