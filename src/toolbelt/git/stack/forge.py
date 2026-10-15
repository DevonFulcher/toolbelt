"""The single external seam: asking the code host about a branch's PR.

Only the forge (GitHub, etc.) can authoritatively answer "did this branch's PR
land?" — a squash-merge rewrites the SHA, so local ancestry can't tell — and
"does this branch have an open PR at all (draft or not), and does it conflict
with its base?", which gate whether `sync` pushes automatically. All are defined as a protocol and injected into
`sync` so tests pass a fake instead of shelling out to `gh`.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

import typer

from toolbelt import logged_process
from toolbelt.git.stack.store import PrCandidate, PrLink
from toolbelt.logger import logger


class Forge(Protocol):
    """A code host that can report on a branch's PR."""

    async def pr_is_merged(self, branch: str) -> bool: ...

    async def pr_is_open(self, branch: str) -> bool: ...

    async def pr_is_conflicting(self, branch: str) -> bool:
        """Whether the branch's open PR currently has merge conflicts with
        its base, per the forge."""
        ...

    async def open_pr_base(self, branch: str) -> tuple[int, str] | None:
        """``(number, base branch)`` of the branch's open PR, or None."""
        ...

    async def set_pr_base(self, number: int, base: str) -> None:
        """Retarget PR ``number`` onto ``base``."""
        ...

    async def pr_candidates(self, branch: str) -> list[PrCandidate]:
        """Every PR (any state) whose head is ``branch``, best first: the
        identity ``store.StackStore.link_pr`` records for the branch's task."""
        ...


def candidate_from_json(node: dict) -> PrCandidate:
    """A ``gh ... --json number,id,createdAt`` PR object."""
    return PrCandidate(
        link=PrLink(number=node["number"], node_id=node["id"]),
        created_at=datetime.fromisoformat(node["createdAt"]),
    )


def ranked_candidates(prs: list[dict]) -> list[PrCandidate]:
    """``gh pr list --json number,id,state,createdAt`` objects, open PRs
    first, then merged ones, then closed ones; newest first within each."""
    order = {"OPEN": 0, "MERGED": 1}
    ranked = sorted(prs, key=lambda pr: pr["createdAt"], reverse=True)
    ranked.sort(key=lambda pr: order.get(pr["state"], 2))
    return [candidate_from_json(pr) for pr in ranked]


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

    async def pr_is_conflicting(self, branch: str) -> bool:
        process = await logged_process.create_subprocess_exec(
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "mergeable",
            "--jq",
            '[.[] | select(.mergeable == "CONFLICTING")] | length',
            cwd=self._root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(
                f"`gh` failed checking conflicts of '{branch}': "
                f"{stderr.decode().strip()}"
            )
            raise typer.Exit(1)
        return stdout.decode().strip() not in ("", "0")

    async def open_pr_base(self, branch: str) -> tuple[int, str] | None:
        process = await logged_process.create_subprocess_exec(
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,baseRefName",
            cwd=self._root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(
                f"`gh` failed reading the open PR of '{branch}': "
                f"{stderr.decode().strip()}"
            )
            raise typer.Exit(1)
        prs = json.loads(stdout)
        return (prs[0]["number"], prs[0]["baseRefName"]) if prs else None

    async def set_pr_base(self, number: int, base: str) -> None:
        process = await logged_process.create_subprocess_exec(
            "gh",
            "pr",
            "edit",
            str(number),
            "--base",
            base,
            cwd=self._root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(
                f"`gh` failed retargeting PR #{number} onto '{base}': "
                f"{stderr.decode().strip()}"
            )
            raise typer.Exit(1)

    async def pr_candidates(self, branch: str) -> list[PrCandidate]:
        process = await logged_process.create_subprocess_exec(
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "20",
            "--json",
            "number,id,state,createdAt",
            cwd=self._root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(
                f"`gh` failed listing the PRs of '{branch}': {stderr.decode().strip()}"
            )
            raise typer.Exit(1)
        return ranked_candidates(json.loads(stdout))
