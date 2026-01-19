import sys
from pathlib import Path
from uuid import UUID

import typer
from pydantic import BaseModel, ConfigDict

from toolbelt.logger import logger
from toolbelt.repos import AfterFileEditHook, repo_for_path

cursor_typer = typer.Typer(help="Hook entrypoints for Cursor events")


class CursorFileEdit(BaseModel):
    model_config = ConfigDict(extra="allow")

    old_string: str
    new_string: str
    file_path: Path | None = None


class CursorHookPayloadBase(BaseModel):
    model_config = ConfigDict(extra="allow")

    conversation_id: UUID
    generation_id: UUID
    model: str
    hook_event_name: str
    cursor_version: str
    workspace_roots: list[Path]
    user_email: str | None = None


class CursorAfterFileEditHookPayload(CursorHookPayloadBase):
    model_config = ConfigDict(extra="allow")

    file_path: Path
    edits: list[CursorFileEdit]


@cursor_typer.command(
    name="after-file-edit",
    help="Handle after file edit events.",
)
def after_file_edit() -> None:
    payload_text = sys.stdin.read()
    parsed = CursorAfterFileEditHookPayload.model_validate_json(payload_text)

    repo = repo_for_path(file_path=parsed.file_path)
    if repo is None:
        logger.info(f"No repo found for file path: {parsed.file_path}")
        return
    hook = next(
        (hook for hook in repo.hooks() if isinstance(hook, AfterFileEditHook)),
        None,
    )
    if hook is None:
        logger.info(f"No after-file-edit hook configured for repo: {repo.name()}")
        return
    hook.run(file_path=parsed.file_path)
