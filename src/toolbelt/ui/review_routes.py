"""The diff review's routes: the files changed between two revisions of a
tracked repo, a file's structural diff, and a language server's answers about
its code."""

from fastapi import APIRouter, HTTPException

from toolbelt.diff_review.difftastic import structural_diff
from toolbelt.diff_review.lsp import LspError, LspSessions
from toolbelt.diff_review.revisions import RevisionError, RevisionPair, Side
from toolbelt.git.stack.store import StackStore, default_db_path
from toolbelt.ui import api_models as api


def _pair(repo_id: int, base: str, head: str) -> RevisionPair:
    store = StackStore(default_db_path())
    try:
        paths = {repo.id: repo.path for repo in store.repos()}
    finally:
        store.close()
    if repo_id not in paths:
        raise HTTPException(status_code=404, detail=f"no repo {repo_id}")
    try:
        return RevisionPair(paths[repo_id], base, head)
    except RevisionError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


def review_router(sessions: LspSessions) -> APIRouter:
    router = APIRouter(prefix="/api/review")

    @router.get("/files", response_model=list[api.ReviewChangedFile])
    def files(repo: int, base: str, head: str) -> list[api.ReviewChangedFile]:
        try:
            return _pair(repo, base, head).changed_files()
        except RevisionError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err

    @router.get("/file", response_model=api.ReviewFile)
    def file(
        repo: int,
        base: str,
        head: str,
        status: api.ReviewStatus,
        path: str,
        old_path: str | None = None,
    ) -> api.ReviewFile:
        pair = _pair(repo, base, head)
        try:
            old_text = "" if status == "A" else pair.text(Side.OLD, old_path or path)
            new_text = "" if status == "D" else pair.text(Side.NEW, path)
        except RevisionError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        return api.ReviewFile(
            path=path,
            old_path=old_path,
            status=status,
            old_text=old_text,
            new_text=new_text,
            difftastic=structural_diff(path, old_text=old_text, new_text=new_text),
        )

    @router.get("/text", response_model=str)
    def text(repo: int, base: str, head: str, side: api.ReviewSide, path: str) -> str:
        try:
            return _pair(repo, base, head).text(Side(side), path)
        except RevisionError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err

    @router.get("/hover", response_model=api.ReviewHover)
    def hover(
        repo: int,
        base: str,
        head: str,
        side: api.ReviewSide,
        path: str,
        line: int,
        character: int,
    ) -> api.ReviewHover:
        pair = _pair(repo, base, head)
        try:
            return sessions.session(pair, Side(side)).hover(path, line, character)
        except (RevisionError, LspError) as err:
            raise HTTPException(status_code=502, detail=str(err)) from err

    @router.get("/definition", response_model=api.ReviewDefinition)
    def definition(
        repo: int,
        base: str,
        head: str,
        side: api.ReviewSide,
        path: str,
        line: int,
        character: int,
    ) -> api.ReviewDefinition:
        pair = _pair(repo, base, head)
        try:
            return sessions.session(pair, Side(side)).definition(path, line, character)
        except (RevisionError, LspError) as err:
            raise HTTPException(status_code=502, detail=str(err)) from err

    return router
