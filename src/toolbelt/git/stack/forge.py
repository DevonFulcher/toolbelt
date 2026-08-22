"""The single external seam: asking the code host whether a PR has merged.

Only the forge (GitHub, etc.) can authoritatively answer "did this branch's PR
land?" — a squash-merge rewrites the SHA, so local ancestry can't tell. This is
defined as a protocol and injected into `sync` so tests pass a fake instead of
shelling out to `gh`.
"""

import asyncio
from pathlib import Path
from typing import Protocol

import typer

from toolbelt.logger import logger


class Forge(Protocol):
    """A code host that can report whether a branch's PR has merged."""

    async def pr_is_merged(self, branch: str) -> bool: ...


class GhForge:
    """`Forge` backed by the GitHub CLI (`gh`)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def pr_is_merged(self, branch: str) -> bool:
        # Query by head branch: the PR record persists even after the remote
        # branch is deleted on merge. A non-empty merged list means it landed.
        # Async so `sync_stack` can check every branch in the stack concurrently
        # instead of paying one network round-trip per branch, serially.
        process = await asyncio.create_subprocess_exec(
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
