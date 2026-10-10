"""One-way mirror of our stack lineage to GitHub's native stacked PRs.

The lineage in the SQLite store stays the source of truth; nothing is read back
from GitHub (or from the extension's own ``.git/gh-stack`` state). After a
successful sync, ``mirror_stack`` publishes each stack's chain with the `gh
stack` extension — `gh stack link <branch>...`, bottom to top, which pushes the
branches, creates missing PRs and fixes bases, additively. To keep GitHub
writes to when they are needed:

- Only a chain of >= 2 branches with open, non-draft PRs is published, and only
  the run of such branches starting at the bottom of the stack (a link's bottom
  branch is based on the trunk, so it must be the stack's real bottom).
- A tree with siblings has one chain per leaf. Only one is published per tree:
  the one with the longest such run, first by leaf name on ties. A branch
  cannot sit in two GitHub stacks, so the other chains are left unlinked.
- The last published chain is stored per repo and bottom; if it is unchanged
  nothing is called.
- Stale membership (a branch dropped from the chain) is never removed with
  `gh stack unstack`: it cannot be told apart from state we did not create, so
  it is logged and left.
- If the repo lacks the feature (exit code 9) or the extension is missing, that
  is recorded with a timestamp and mirroring is skipped until ``UNSUPPORTED_TTL``
  passes.
"""

import asyncio
import hashlib
import itertools
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import typer

from toolbelt import logged_process
from toolbelt.git.stack.lineage import (
    Children,
    all_parents,
    children_map,
    get_gh_stack_unsupported_at,
    get_published_chain,
    set_gh_stack_unsupported_at,
    set_published_chain,
)
from toolbelt.logger import logger

UNSUPPORTED_TTL = timedelta(hours=24)

# `gh stack` exits with this when stacked PRs are not enabled for the repo.
_NOT_ENABLED_EXIT_CODE = 9


class StackedPrs(Protocol):
    """GitHub's native stacked PRs, as far as the mirror needs them."""

    async def pr_is_ready(self, branch: str, *, cwd: Path) -> bool:
        """True if ``branch`` has an open, non-draft PR."""
        ...

    def link(self, branches: list[str], *, base: str, cwd: Path) -> bool:
        """Link ``branches`` (bottom to top) as one stack whose bottom is
        based on ``base``. Returns ``False`` if stacked PRs are unavailable
        (not enabled, extension missing)."""
        ...


class GhStackedPrs:
    """`StackedPrs` backed by the GitHub CLI and its `gh stack` extension."""

    async def pr_is_ready(self, branch: str, *, cwd: Path) -> bool:
        process = await logged_process.create_subprocess_exec(
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "isDraft",
            "--jq",
            "map(select(.isDraft | not)) | length",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(
                f"`gh` failed checking PR readiness of '{branch}': "
                f"{stderr.decode().strip()}"
            )
            raise typer.Exit(1)
        return stdout.decode().strip() not in ("", "0")

    def link(self, branches: list[str], *, base: str, cwd: Path) -> bool:
        # Without --base, `gh stack link` retargets the bottom PR onto the
        # repo's default branch, which is wrong for stacks rooted elsewhere.
        result = logged_process.run(
            ["gh", "stack", "link", "--base", base, *branches],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        # `gh` reports a missing extension as an unknown `stack` subcommand.
        if (
            result.returncode == _NOT_ENABLED_EXIT_CODE
            or 'unknown command "stack"' in result.stderr
        ):
            return False
        logger.error(f"`gh stack link` failed: {result.stderr.strip()}")
        raise typer.Exit(1)


def _chains(top: str, children: Children) -> list[list[str]]:
    """Every root -> leaf path of the tree under ``top``, in leaf-name order."""
    kids = children.get(top, [])
    if not kids:
        return [[top]]
    return [[top, *rest] for kid in kids for rest in _chains(kid, children)]


def _chain_hash(branches: list[str]) -> str:
    return hashlib.sha256("\n".join(branches).encode()).hexdigest()


async def _ready(
    branches: list[str], root: Path, stacked_prs: StackedPrs
) -> dict[str, bool]:
    results = await asyncio.gather(
        *(stacked_prs.pr_is_ready(b, cwd=root) for b in branches)
    )
    return dict(zip(branches, results))


def mirror_stack(
    stack: list[str],
    *,
    root: Path,
    stacked_prs: StackedPrs,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> None:
    """Publish the chain of each tree among ``stack`` to GitHub, if needed.

    ``stack`` is the branches just synced; those no longer tracked (landed and
    removed) are ignored. ``root`` must be an existing worktree.
    """
    unsupported_at = get_gh_stack_unsupported_at(root=root)
    if unsupported_at is not None and now() - unsupported_at < UNSUPPORTED_TTL:
        logger.info("GitHub stacked PRs unavailable for this repo; skipping mirror.")
        return

    parents = all_parents(root=root)
    children = children_map(parents)
    tops = [b for b in stack if b in parents and parents[b] not in parents]

    for top in tops:
        chains = _chains(top, children)
        if max(map(len, chains)) < 2:
            continue
        members = sorted({branch for chain in chains for branch in chain})
        ready = asyncio.run(_ready(members, root, stacked_prs))
        # max() keeps the first of equal runs, i.e. the first leaf by name.
        publish = max(
            (list(itertools.takewhile(lambda b: ready[b], c)) for c in chains),
            key=len,
        )
        if len(publish) < 2:
            continue
        if len(chains) > 1:
            logger.info(
                f"'{top}' has {len(chains)} chains; mirroring only "
                f"{' -> '.join(publish)} to GitHub stacked PRs."
            )

        chain_hash = _chain_hash(publish)
        published = get_published_chain(top, root=root)
        if published is not None and published.chain_hash == chain_hash:
            continue
        if published is not None:
            dropped = set(published.branches) - set(publish)
            if dropped:
                logger.info(
                    f"Not unstacking {sorted(dropped)} from GitHub's stack for "
                    f"'{top}'; remove them there if unwanted."
                )

        if not stacked_prs.link(publish, base=parents[top], cwd=root):
            set_gh_stack_unsupported_at(now(), root=root)
            logger.info(
                "GitHub stacked PRs are not enabled for this repo (or `gh stack` "
                f"is not installed); skipping mirror for {UNSUPPORTED_TTL}."
            )
            return
        if unsupported_at is not None:
            set_gh_stack_unsupported_at(None, root=root)
            unsupported_at = None
        set_published_chain(top, chain_hash, publish, now(), root=root)
        logger.info(f"Mirrored {' -> '.join(publish)} to GitHub stacked PRs.")
