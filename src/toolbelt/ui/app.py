"""The `tt ui` web app: every known repo's stacks, read-only.

The index renders the branch trees straight from the stack DB (no network), and
each branch carries an htmx placeholder that fetches its PR/CI/review status
from ``/repos/{id}/status`` — the same lookup `tt g tree` uses
(``status.fetch_branch_status``). Placeholders fire concurrently, so the page is
usable immediately and statuses fill in as ``gh`` answers. Nothing here writes.

The status lookup and the Jira base URL are FastAPI dependencies
(``status_fetcher``, ``jira_base_url``) so tests can override them.
"""

import re
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
from toolbelt.ui import branch_links
from toolbelt.ui.branch_links import Link

_HERE = Path(__file__).parent
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")

# (branch, repo path) -> that branch's PR status.
StatusFetcher = Callable[[str, Path], Awaitable[BranchStatus]]


def status_fetcher() -> StatusFetcher:
    async def fetch(branch: str, root: Path) -> BranchStatus:
        return await fetch_branch_status(branch, root=root)

    return fetch


def jira_base_url() -> str | None:
    return branch_links.jira_base_url(default_config_path())


@dataclass(frozen=True)
class BranchRow:
    name: str
    depth: int
    # False for the untracked base branch (e.g. ``main``): no status, no links.
    tracked: bool
    current: bool
    jira: Link | None
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


def _repo_views(cwd_root: Path | None, *, base_url: str | None) -> list[RepoView]:
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
                rows.append(
                    BranchRow(
                        name=name,
                        depth=depth,
                        tracked=name in parents,
                        current=name == current,
                        jira=branch_links.jira_link(
                            links.jira if links else None, base_url=base_url
                        ),
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


def create_app(cwd_root: Path | None = None) -> FastAPI:
    """``cwd_root``: the repo `tt ui` was launched from; its checked-out
    branch is marked current, as `tt g tree` does."""
    app = FastAPI(title="toolbelt", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request, base_url: str | None = Depends(jira_base_url)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"repos": _repo_views(cwd_root, base_url=base_url)},
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
