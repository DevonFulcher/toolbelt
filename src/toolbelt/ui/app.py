"""The `tt ui` web app: every known repo's stacks, live, read-only.

The server is a JSON API plus the built web client (``web/``, served from
``static/dist``):

* ``GET /api/snapshot``: every tracked branch of every repo (stack structure,
  links, live status, the state catalog), straight from the stack DB plus the
  in-memory caches (no network on the request path); see ``snapshot.py`` and
  ``api_models.py``;
* ``GET /api/events``: a Server-Sent Events stream of typed events that keep a
  snapshot current:

  - ``hello``: sent on connect with the current event version, so a browser
    that reconnects knows whether it missed anything;
  - ``status``: one branch's new live status (also sent for the branches
    stacked on it, whose next action depends on it);
  - ``task``: a branch's task title/status was (re)fetched;
  - ``structure``: the stack DB was written (new branch, re-parenting, new
    links), so the page re-fetches the snapshot;
  - ``refreshed``: a polling pass finished (drives "updated Ns ago");
  - ``untracked``: the list of the user's open PRs that are not tracked
    branches changed (see ``untracked_prs.py``).

A background ``StatusRefresher`` (``live_status.py``) keeps the status cache
current by polling GitHub (one GraphQL call per branch, see ``pr_fetch.py``)
while any browser is connected; a ``TaskFeed`` does the same for tasks and
an ``UntrackedPrFeed`` (slowly) for the PRs toolbelt does not track.

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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack.store import Parents, StackStore, default_db_path
from toolbelt.tasks.config import configured_tracker
from toolbelt.tasks.tracker import TaskTracker
from toolbelt.ui import api_models as api
from toolbelt.ui import branch_links
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

_HERE = Path(__file__).parent
#: The web client's build output (``pnpm build`` in ``web/``).
CLIENT_DIST = _HERE / "static" / "dist"

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
    ``remote_of`` looks up a checkout's origin: see ``untracked_prs.py``."""
    task_cache = TaskInfoCache(ttl_seconds=task_ttl_seconds, clock=clock)
    hub = EventHub(clock=clock, grace=grace_seconds)

    def publish_status(branch: TrackedBranch, fetched: Fetched) -> None:
        # The cache already holds ``fetched``, so parent contexts see it.
        parents = _all_parents(branch.repo_id)
        now = wall_clock()
        for name in descendants(parents, branch.branch):
            status = live_status(
                refresher.get(branch.repo_id, name),
                parent_context(branch.repo_id, parents, name, refresher),
                now,
            )
            hub.publish(
                "status",
                {
                    "branch_id": row_id(branch.repo_id, name),
                    "status": status.model_dump(mode="json"),
                },
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
            asyncio.create_task(untracked_feed.run()),
            asyncio.create_task(watcher.run()),
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
    app.state.untracked_feed = untracked_feed
    app.state.structure_watcher = watcher

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
    def snapshot() -> api.Snapshot:
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
        )

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
