"""The `tt ui` web app: every known repo's stacks, read-only.

The index renders the branch trees straight from the stack DB (no network), and
each branch carries an htmx placeholder that fetches its PR/CI/review status
from ``/repos/{id}/status`` — the same lookup `tt g tree` uses
(``status.fetch_branch_status``). Placeholders fire concurrently, so the page is
usable immediately and statuses fill in as ``gh`` answers. Nothing here writes.

Each branch's task cell works the same way: it renders the stored ref
immediately (linked via the configured task tracker) and a placeholder fetches
the task's title and status from ``/repos/{id}/task`` through a short TTL cache.

The status lookup and the task tracker are FastAPI dependencies
(``status_fetcher``, ``task_tracker``) so tests can override them.
"""

import functools
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import typer
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack import lineage
from toolbelt.git.stack.status import BranchStatus, fetch_branch_status
from toolbelt.git.stack.store import Parents, RepoRecord, StackStore, default_db_path
from toolbelt.git.worktrees import current_branch
from toolbelt.tasks.config import configured_tracker
from toolbelt.tasks.tracker import TaskInfo, TaskTracker
from toolbelt.ui import branch_links
from toolbelt.ui.branch_links import Link
from toolbelt.ui.task_cache import TaskInfoCache

_HERE = Path(__file__).parent
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")

# (branch, repo path) -> that branch's PR status.
StatusFetcher = Callable[[str, Path], Awaitable[BranchStatus]]


def status_fetcher() -> StatusFetcher:
    async def fetch(branch: str, root: Path) -> BranchStatus:
        return await fetch_branch_status(branch, root=root)

    return fetch


@functools.cache
def _tracker_from_config(config_file: Path) -> TaskTracker | None:
    # Built once per config path so the tracker's HTTP client and resolved
    # token are reused across requests.
    return configured_tracker(config_file)


def task_tracker() -> TaskTracker | None:
    return _tracker_from_config(default_config_path())


@dataclass(frozen=True)
class BranchRow:
    name: str
    depth: int
    # False for the untracked base branch (e.g. ``main``): no status, no links.
    tracked: bool
    current: bool
    task: Link | None
    # True when the configured tracker owns the task, so its title/status can
    # be fetched lazily.
    task_fetchable: bool
    agent: Link | None


@dataclass(frozen=True)
class RepoView:
    id: int
    name: str
    path: Path
    path_missing: bool
    rows: list[BranchRow]


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


def _repo_views(
    cwd_root: Path | None, *, tracker: TaskTracker | None
) -> list[RepoView]:
    cwd_common_dir = (
        lineage.repo_identity(cwd_root).git_common_dir if cwd_root else None
    )
    store = StackStore(default_db_path())
    try:
        views = []
        for repo in store.repos():
            parents = store.all_parents(repo.id)
            if not parents:
                continue
            path_missing = not repo.path.is_dir()
            current = (
                _checked_out_branch(cwd_root)
                if cwd_root and repo.git_common_dir == cwd_common_dir
                else None
            )
            rows = []
            for name, depth in _walk(parents):
                links = store.get_links(repo.id, name)
                owner = branch_links.owning_tracker(
                    links.task_tracker if links else None, tracker
                )
                rows.append(
                    BranchRow(
                        name=name,
                        depth=depth,
                        tracked=name in parents,
                        current=name == current,
                        task=branch_links.task_link(
                            links.task if links else None, tracker=owner
                        ),
                        task_fetchable=owner is not None and bool(links and links.task),
                        agent=branch_links.agent_link(
                            links.agent_link if links else None
                        ),
                    )
                )
            views.append(
                RepoView(
                    id=repo.id,
                    name=repo.name,
                    path=repo.path,
                    path_missing=path_missing,
                    rows=rows,
                )
            )
        return views
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


def _tracked_repo(repo_id: int, branch: str) -> RepoRecord:
    """The repo, 404 unless ``branch`` is tracked in it (so only known
    branch names ever reach ``gh``)."""
    store = StackStore(default_db_path())
    try:
        for repo in store.repos():
            if repo.id == repo_id and branch in store.all_parents(repo.id):
                return repo
    finally:
        store.close()
    raise HTTPException(status_code=404, detail=f"{branch} not tracked in {repo_id}")


def create_app(
    cwd_root: Path | None = None,
    *,
    task_ttl_seconds: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """``cwd_root``: the repo `tt ui` was launched from; its checked-out
    branch is marked current, as `tt g tree` does. Fetched task info is cached
    for ``task_ttl_seconds``."""
    task_cache = TaskInfoCache(ttl_seconds=task_ttl_seconds, clock=clock)
    app = FastAPI(title="toolbelt", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request, tracker: TaskTracker | None = Depends(task_tracker)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"repos": _repo_views(cwd_root, tracker=tracker)},
        )

    @app.get("/repos/{repo_id}/task", response_class=HTMLResponse)
    def task(
        request: Request,
        repo_id: int,
        branch: str,
        tracker: TaskTracker | None = Depends(task_tracker),
    ) -> HTMLResponse:
        _tracked_repo(repo_id, branch)
        ref, stored_tracker = _task_ref(repo_id, branch)
        owner = branch_links.owning_tracker(stored_tracker, tracker)
        info: TaskInfo | None = None
        if owner is not None:
            info = task_cache.get(owner, ref)
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
            },
        )

    @app.get("/repos/{repo_id}/status", response_class=HTMLResponse)
    async def status(
        request: Request,
        repo_id: int,
        branch: str,
        fetch: StatusFetcher = Depends(status_fetcher),
    ) -> HTMLResponse:
        repo = _tracked_repo(repo_id, branch)
        result = None
        if repo.path.is_dir():
            result = await fetch(branch, repo.path)
        pr_number = None
        if result is not None and result.url:
            match = _PR_NUMBER_RE.search(result.url)
            pr_number = match.group(1) if match else None
        return templates.TemplateResponse(
            request,
            "status.html",
            {
                "repo_id": repo_id,
                "branch": branch,
                "status": result,
                "pr_number": pr_number,
                "path_missing": not repo.path.is_dir(),
            },
        )

    return app
