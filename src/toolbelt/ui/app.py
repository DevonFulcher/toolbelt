"""The `tt ui` web app: every known repo's stacks in one live table, read-only.

The index renders every tracked branch of every repo as a row of one table,
straight from the stack DB plus the in-memory status cache (no network on the
request path). A background ``StatusRefresher`` (``live_status.py``) keeps that
cache current by polling GitHub (one GraphQL call per branch, see
``pr_fetch.py``) while any browser is connected, and the page
receives changes over a Server-Sent Events stream (``/events``):

* ``status``: one branch's status cells (and its card), as HTML, swapped in
  by id;
* ``structure``: the stack DB was written (new branch, re-parenting, new
  links), so the page re-fetches ``/`` and swaps in the new rows;
* ``refreshed``: a polling pass finished (drives the "updated Ns ago" label);
* ``hello``: sent on connect with the current event version, so a browser that
  reconnects knows whether it missed anything.

Each status column (next action, PR, CI, review, comments, merge) is its own
cell. The same branches are also rendered as cards (``view=stacked``: by repo
and stack; ``view=grouped``: by next action), each with
its own timeline track: the page script lays those out from the table's data,
so the table stays the one source of truth for filtering and sorting. The
trunk (``main``) is not a tracked branch and is never rendered; each stack
starts at its first real branch. Sorting and filtering are client-side
(``static/stack-table.js``): cells carry their
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
from datetime import UTC, datetime
from pathlib import Path

import typer
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack import lineage
from toolbelt.git.stack.store import Parents, StackStore, default_db_path
from toolbelt.git.worktrees import current_branch
from toolbelt.tasks.config import configured_tracker
from toolbelt.tasks.tracker import TaskInfo, TaskTracker
from toolbelt.ui import branch_links, pr_states
from toolbelt.ui.branch_links import Link
from toolbelt.ui.next_action import ParentContext, next_action
from toolbelt.ui.pr_fetch import PrSnapshot, fetch_snapshot
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
    ("next", "Next"),
    ("pr", "PR"),
    ("ci", "CI"),
    ("review", "Review"),
    ("comments", "Comments"),
    ("merge", "Merge"),
    ("task", "Task"),
    ("session", "Session"),
]
_FILTER_LABELS = {
    "next": "Next",
    "pr": "PR",
    "ci": "CI",
    "review": "Review",
    "comments": "Comments",
    "merge": "Merge",
}

#: The Next filter's friendly option texts. The option *values* stay the raw
#: who (what the cell's data-value, the sort and the URL use).
_NEXT_OPTION_LABELS = {
    "you": "Needs me",
    "ci": "Waiting on CI",
    "others": "Waiting on others",
    "none": "Done",
}

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
    #: Stack depth: 0 for a branch directly on the trunk.
    depth: int
    #: Row id of the parent branch; ``""`` for a branch on the trunk, which is
    #: not a tracked branch and never rendered.
    parent_id: str
    #: The tracked branch this one is stacked on (``""`` on the trunk).
    parent: str
    # Position in the page's depth-first order: the default sort.
    order: int
    current: bool
    cells: StatusCells
    #: Position in its stack (``stack_pos`` of ``stack_height``): depth + 1,
    #: and the height of the tallest chain in the stack this branch is in.
    stack_pos: int
    stack_height: int
    #: ``(row id, branch)`` of the tracked branches stacked on this one.
    children: tuple[tuple[str, str], ...]
    task: Link | None
    # True when the configured tracker owns the task, so its title/status can
    # be fetched lazily.
    task_fetchable: bool
    agent: Link | None


def _walk(parents: Parents) -> list[tuple[str, int]]:
    """Every tracked branch depth-first as ``(name, depth)``. The trunk(s)
    the stacks hang off are not tracked branches and are left out, so a
    stack starts at depth 0 with its first real branch."""
    children = lineage.children_map(parents)
    order: list[tuple[str, int]] = []

    def visit(branch: str, depth: int) -> None:
        order.append((branch, depth))
        for child in children.get(branch, []):
            visit(child, depth + 1)

    for base in lineage.roots(parents):
        for first in children.get(base, []):
            visit(first, 0)
    return order


def _stack_heights(order: list[tuple[str, int]]) -> dict[str, int]:
    """Each branch -> the height of its stack (the deepest chain below the
    stack's first branch, counted in branches), from the depth-first order."""
    heights: dict[str, int] = {}
    group: list[str] = []
    deepest = 0
    for name, depth in [*order, ("", 0)]:
        if depth == 0:
            for member in group:
                heights[member] = deepest
            group, deepest = [], 0
        group.append(name)
        deepest = max(deepest, depth + 1)
    return heights


def _checked_out_branch(root: Path) -> str | None:
    try:
        return current_branch(root)
    except typer.Exit:
        # Detached HEAD (common in worktrees): nothing to mark.
        return None


def _parent_context(
    repo_id: int, parents: Parents, branch: str, refresher: StatusRefresher
) -> ParentContext | None:
    """The tracked branch ``branch`` is stacked on, with its PR state and who
    its own next action waits on (which depends on *its* parent in turn); ``None``
    for a branch on the base branch, or while the parent's status is unknown."""
    parent = parents.get(branch)
    if parent is None or parent not in parents:
        return None
    fetched = refresher.get(repo_id, parent)
    if fetched is None or fetched.status is None:
        return None
    parent_action = next_action(
        fetched.status, _parent_context(repo_id, parents, parent, refresher)
    )
    return ParentContext(name=parent, pr=fetched.status.pr, next_who=parent_action.who)


def _cells_for(
    fetched: Fetched | None, parent: ParentContext | None, now: datetime
) -> StatusCells:
    if fetched is None:
        return UNLOADED
    return status_cells(
        fetched.status,
        failed=fetched.status is None,
        parent=parent,
        facts=fetched.facts,
        now=now,
    )


def _page_rows(
    cwd_root: Path | None,
    *,
    tracker: TaskTracker | None,
    refresher: StatusRefresher,
    now: datetime,
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
            order = _walk(parents)
            heights = _stack_heights(order)
            kids = lineage.children_map(parents)
            for name, depth in order:
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
                        parent_id=(
                            row_id(repo.id, parent) if parent in parents else ""
                        ),
                        parent=parent if parent in parents else "",
                        order=len(rows),
                        stack_pos=depth + 1,
                        stack_height=heights[name],
                        children=tuple(
                            (row_id(repo.id, child), child)
                            for child in kids.get(name, [])
                        ),
                        current=name == current,
                        cells=_cells_for(
                            refresher.get(repo.id, name),
                            _parent_context(repo.id, parents, name, refresher),
                            now,
                        ),
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
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
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
    ``clock`` times the polling and caches; ``wall_clock`` (UTC) dates the
    ages shown. Fetched task info is cached for ``task_ttl_seconds``. Polling carries on
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
    card_template = templates.env.get_template("card_status.html")

    def publish_cells(
        repo_id: int,
        branch: str,
        fetched: Fetched | None,
        parent: ParentContext | None,
    ) -> None:
        c = _cells_for(fetched, parent, wall_clock())
        hub.publish(
            "status",
            {
                "html": status_template.render(
                    repo_id=repo_id, branch=branch, c=c, path_missing=False
                ),
                "card": card_template.render(
                    repo_id=repo_id, branch=branch, c=c, path_missing=False
                ),
            },
        )

    def publish_status(branch: TrackedBranch, fetched: Fetched) -> None:
        # The cache already holds ``fetched``, so parent contexts see it.
        parents = _all_parents(branch.repo_id)
        # A branch stacked on this one (at any depth) has its next action
        # depend on this PR's state ("Blocked on <parent>", and who that waits
        # on follows the parent's own next action), so the cells of all its
        # descendants are republished too.
        names = [branch.branch]
        for name in names:
            names.extend(child for child, parent in parents.items() if parent == name)
        for name in names:
            publish_cells(
                branch.repo_id,
                name,
                refresher.get(branch.repo_id, name),
                _parent_context(branch.repo_id, parents, name, refresher),
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
        repos, rows = _page_rows(
            cwd_root, tracker=tracker, refresher=refresher, now=wall_clock()
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": version,
                "state_catalog": pr_states.catalog_json().replace("</", "<\\/"),
                "repos": repos,
                "rows": rows,
                "columns": COLUMNS,
                "filters": [
                    (
                        key,
                        _FILTER_LABELS[key],
                        [
                            (o, _NEXT_OPTION_LABELS.get(o, o) if key == "next" else o)
                            for o in options
                        ],
                    )
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
