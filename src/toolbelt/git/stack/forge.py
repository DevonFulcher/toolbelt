"""The single external seam: asking the code host about a branch's PR.

Only the forge (GitHub, etc.) can authoritatively answer "did this branch's PR
land?" — a squash-merge rewrites the SHA, so local ancestry can't tell — and
"does this branch have an open PR at all (draft or not)?", which gates whether
`sync` pushes automatically. Both are defined as a protocol and injected into
`sync` so tests pass a fake instead of shelling out to `gh`.
"""

import asyncio
from pathlib import Path
from typing import Protocol

import typer

from toolbelt import logged_process
from toolbelt.logger import logger


class Forge(Protocol):
    """A code host that can report on a branch's PR."""

    async def pr_is_merged(self, branch: str) -> bool: ...

    async def pr_is_open(self, branch: str) -> bool: ...


class GhForge:
    """`Forge` backed by the GitHub CLI (`gh`)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def pr_is_merged(self, branch: str) -> bool:
        # Query by head branch: the PR record persists even after the remote
        # branch is deleted on merge. A non-empty merged list means it landed.
        # Async so `sync_stack` can check every branch in the stack concurrently
        # instead of paying one network round-trip per branch, serially.
        process = await logged_process.create_subprocess_exec(
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "merged",
            "--json",
            "number",
            "--jq",
            "length",
            cwd=self._root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            # Don't degrade silently to "not merged" — a gh failure (auth,
            # network, missing CLI) would suppress all restacking. Crash loudly.
            logger.error(
                f"`gh` failed checking merge status of '{branch}': "
                f"{stderr.decode().strip()}"
            )
            raise typer.Exit(1)
        return stdout.decode().strip() not in ("", "0")

    async def pr_is_open(self, branch: str) -> bool:
        # Draft or not — just "does anyone have a reason to look at a push
        # to this branch", which decides whether `sync` pushes automatically.
        process = await logged_process.create_subprocess_exec(
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            "length",
            cwd=self._root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            # Same reasoning as pr_is_merged: crash rather than silently
            # treat a `gh` failure as "no PR, don't bother pushing".
            logger.error(
                f"`gh` failed checking open status of '{branch}': "
                f"{stderr.decode().strip()}"
            )
            raise typer.Exit(1)
        return stdout.decode().strip() not in ("", "0")
