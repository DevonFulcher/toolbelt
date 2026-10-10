"""The `tt ui` web app: every known repo's stacks in one live table, read-only.

The index renders every tracked branch of every repo as a row of one table,
straight from the stack DB plus the in-memory status cache (no network on the
request path). A background ``StatusRefresher`` (``live_status.py``) keeps that
cache current by polling GitHub through the same lookup `tt g tree` uses
(``status.fetch_branch_status``) while any browser is connected, and the page
receives changes over a Server-Sent Events stream (``/events``):

* ``status``: one branch's five status cells, as HTML, swapped in by id;
* ``structure``: the stack DB was written (new branch, re-parenting, new
  links), so the page re-fetches ``/`` and swaps in the new rows;
* ``refreshed``: a polling pass finished (drives the "updated Ns ago" label);
* ``hello``: sent on connect with the current event version, so a browser that
  reconnects knows whether it missed anything.

Each status column (PR, CI, review, comments, merge) is its own cell. Sorting
and filtering are client-side (``static/stack-table.js``): cells carry their
sort/filter values as ``data-*`` attributes, the chosen view lives in the URL
query string, and the script re-applies it after every live update. Rows are
rendered flat in the DOM with parent pointers; the script does the indentation.

Each branch's task cell renders the stored ref immediately (linked via the
configured task tracker) and an htmx placeholder fetches the task's title and
status from ``/repos/{id}/task`` through a short TTL cache.

The status lookup (``fetch``) and clock are ``create_app`` arguments and the
task tracker is a FastAPI dependency (``task_tracker``), so tests can inject
them.
"""

import asyncio
import contextlib
import functools
import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import typer
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack import lineage
from toolbelt.git.stack.status import BranchStatus, fetch_branch_status
from toolbelt.git.stack.store import Parents, StackStore, default_db_path
from toolbelt.git.worktrees import current_branch
from toolbelt.tasks.config import configured_tracker
from toolbelt.tasks.tracker import TaskInfo, TaskTracker
from toolbelt.ui import branch_links
from toolbelt.ui.branch_links import Link
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
from toolbelt.ui.status_cells import (
    FILTER_OPTIONS,
    UNLOADED,
    StatusCells,
    status_cells,
)
from toolbelt.ui.task_cache import TaskInfoCache

_HERE = Path(__file__).parent

# (column key, header label), in display order.
COLUMNS = [
    ("repo", "Repo"),
    ("branch", "Branch"),
    ("pr", "PR"),
    ("ci", "CI"),
    ("review", "Review"),
    ("comments", "Comments"),
    ("merge", "Merge"),
    ("task", "Task"),
    ("session", "Session"),
]
_FILTER_LABELS = {
    "pr": "PR",
    "ci": "CI",
    "review": "Review",
    "comments": "Comments",
    "merge": "Merge",
}

# Sent when no event has gone out for this long, so intermediaries keep the
# stream open and a dead client is noticed.
_KEEPALIVE_SECONDS = 15.0


async def fetch_status(branch: str, root: Path) -> BranchStatus:
    return await fetch_branch_status(branch, root=root)


@functools.cache
def _tracker_from_config(config_file: Path) -> TaskTracker | None:
    # Built once per config path so the tracker's HTTP client and resolved
    # token are reused across requests.
    return configured_tracker(config_file)


def task_tracker() -> TaskTracker | None:
    return _tracker_from_config(default_config_path())


@dataclass(frozen=True)
class RepoView:
    id: int
    name: str
    path: Path
    path_missing: bool


@dataclass(frozen=True)
class BranchRow:
    #: Unique across repos (the same branch name, e.g. ``main``, can be in
    #: several): the key the page script identifies rows by.
    id: str
    repo: RepoView
    name: str
    depth: int
    parent_id: str
    # Position in the page's depth-first order: the default sort.
    order: int
    # False for the untracked base branch (e.g. ``main``): no status, no links.
    tracked: bool
    current: bool
    cells: StatusCells
    task: Link | None
    # True when the configured tracker owns the task, so its title/status can
    # be fetched lazily.
    task_fetchable: bool
    agent: Link | None


def _walk(parents: Parents) -> list[tuple[str, int]]:
    """Every branch (bases included) depth-first as ``(name, depth)``."""
    children = lineage.children_map(parents)
    order: list[tuple[str, int]] = []

    def visit(branch: str, depth: int) -> None:
        order.append((branch, depth))
        for child in children.get(branch, []):
            visit(child, depth + 1)

    for base in lineage.roots(parents):
        visit(base, 0)
    return order


def _checked_out_branch(root: Path) -> str | None:
    try:
        return current_branch(root)
    except typer.Exit:
        # Detached HEAD (common in worktrees): nothing to mark.
        return None


def _cells_for(fetched: Fetched | None) -> StatusCells:
    if fetched is None:
        return UNLOADED
    return status_cells(fetched.status, failed=fetched.status is None)


def _page_rows(
    cwd_root: Path | None,
    *,
    tracker: TaskTracker | None,
    refresher: StatusRefresher,
) -> tuple[list[RepoView], list[BranchRow]]:
    """Every repo with tracked branches, and all their rows: repos in name
    order, each repo's branches depth-first."""
    cwd_common_dir = (
        lineage.repo_identity(cwd_root).git_common_dir if cwd_root else None
    )
    store = StackStore(default_db_path())
    try:
        repos: list[RepoView] = []
        rows: list[BranchRow] = []
        for repo in sorted(store.repos(), key=lambda r: (r.name, r.id)):
            parents = store.all_parents(repo.id)
            if not parents:
                continue
            view = RepoView(
                id=repo.id,
                name=repo.name,
                path=repo.path,
                path_missing=not repo.path.is_dir(),
            )
            repos.append(view)
            current = (
                _checked_out_branch(cwd_root)
                if cwd_root and repo.git_common_dir == cwd_common_dir
                else None
            )
            for name, depth in _walk(parents):
                links = store.get_links(repo.id, name)
                owner = branch_links.owning_tracker(
                    links.task_tracker if links else None, tracker
                )
                parent = parents.get(name)
                rows.append(
                    BranchRow(
                        id=row_id(repo.id, name),
                        repo=view,
                        name=name,
                        depth=depth,
                        parent_id=row_id(repo.id, parent) if parent else "",
                        order=len(rows),
                        tracked=name in parents,
                        current=name == current,
                        cells=_cells_for(refresher.get(repo.id, name)),
                        task=branch_links.task_link(
                            links.task if links else None, tracker=owner
                        ),
                        task_fetchable=owner is not None and bool(links and links.task),
                        agent=branch_links.agent_link(
                            links.agent_link if links else None
                        ),
                    )
                )
        return repos, rows
    finally:
        store.close()


def row_id(repo_id: int, branch: str) -> str:
    return f"{repo_id}:{branch}"


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


def _task_ref(repo_id: int, branch: str) -> tuple[str, str | None]:
    """``(task ref, tracker it was stored under)`` for a tracked branch that
    has a task, else 404 -- so only stored refs ever reach a tracker."""
    store = StackStore(default_db_path())
    try:
        links = store.get_links(repo_id, branch)
    finally:
        store.close()
    if links is None or not links.task:
        raise HTTPException(status_code=404, detail=f"no task on {branch}")
    return links.task, links.task_tracker


def cell_id(repo_id: int, branch: str, column: str) -> str:
    """A DOM id for one branch's cell: branch names hold characters that are
    awkward in ids (``/``), so they are hashed."""
    digest = hashlib.sha1(branch.encode()).hexdigest()[:12]
    return f"c-{repo_id}-{digest}-{column}"


def _require_tracked(repo_id: int, branch: str) -> None:
    """404 unless ``branch`` is tracked in the repo (so only known branch
    names ever reach a tracker)."""
    store = StackStore(default_db_path())
    try:
        for repo in store.repos():
            if repo.id == repo_id and branch in store.all_parents(repo.id):
                return
    finally:
        store.close()
    raise HTTPException(status_code=404, detail=f"{branch} not tracked in {repo_id}")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def create_app(
    cwd_root: Path | None = None,
    *,
    fetch: StatusFetcher = fetch_status,
    task_ttl_seconds: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
    refresh_interval: float = DEFAULT_INTERVAL_SECONDS,
    settled_refresh_interval: float = DEFAULT_SETTLED_INTERVAL_SECONDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    tick: float = DEFAULT_TICK_SECONDS,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    db_version: Callable[[], int] | None = None,
    keepalive_seconds: float = _KEEPALIVE_SECONDS,
) -> FastAPI:
    """``cwd_root``: the repo `tt ui` was launched from; its checked-out
    branch is marked current, as `tt g tree` does. ``fetch`` looks up a
    branch's PR status; ``db_version`` reports when the stack DB changes
    (default: ``PRAGMA data_version`` on a connection held while serving).
    Fetched task info is cached for ``task_ttl_seconds``. Polling carries on
    for ``grace_seconds`` after the last browser disconnects. The background
    pollers start with the app's lifespan, so an app that is not served (a
    bare ``TestClient``) never polls; tests drive ``app.state.refresher`` and
    ``app.state.structure_watcher`` directly."""
    task_cache = TaskInfoCache(ttl_seconds=task_ttl_seconds, clock=clock)
    hub = EventHub(clock=clock, grace=grace_seconds)
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.globals["cell_id"] = cell_id
    templates.env.globals["UNLOADED"] = UNLOADED
    status_template = templates.env.get_template("status.html")

    def publish_status(branch: TrackedBranch, fetched: Fetched) -> None:
        html = status_template.render(
            repo_id=branch.repo_id,
            branch=branch.branch,
            c=_cells_for(fetched),
            path_missing=False,
        )
        hub.publish("status", {"html": html})

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
    app.state.structure_watcher = watcher
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request, tracker: TaskTracker | None = Depends(task_tracker)
    ) -> HTMLResponse:
        # Read before the rows: an event published meanwhile then makes the
        # page look stale to /events, never the other way round.
        version = hub.version
        repos, rows = _page_rows(cwd_root, tracker=tracker, refresher=refresher)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": version,
                "repos": repos,
                "rows": rows,
                "columns": COLUMNS,
                "filters": [
                    (key, _FILTER_LABELS[key], options)
                    for key, options in FILTER_OPTIONS.items()
                ],
            },
        )

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            queue = hub.subscribe()
            try:
                yield _sse(
                    "hello",
                    {
                        "v": hub.version,
                        "age": refresher.seconds_since_refresh(),
                    },
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

    @app.get("/repos/{repo_id}/task", response_class=HTMLResponse)
    def task(
        request: Request,
        repo_id: int,
        branch: str,
        tracker: TaskTracker | None = Depends(task_tracker),
    ) -> HTMLResponse:
        _require_tracked(repo_id, branch)
        ref, stored_tracker = _task_ref(repo_id, branch)
        owner = branch_links.owning_tracker(stored_tracker, tracker)
        info: TaskInfo | None = None
        unavailable: str | None = None
        if owner is not None:
            info = task_cache.get(owner, ref)
            if info is None:
                unavailable = owner.unavailable_reason() or "task details unavailable"
        return templates.TemplateResponse(
            request,
            "task.html",
            {
                "repo_id": repo_id,
                "branch": branch,
                "task": branch_links.task_link(ref, tracker=owner),
                "fetchable": owner is not None,
                "loading": False,
                "info": info,
                "unavailable": unavailable,
            },
        )

    return app
