"""Compress a branch and mark its draft PR ready before posting it for review."""

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from toolbelt import logged_process
from toolbelt.git.stack.worktree import worktree_paths

OUTPUT_LIMIT = 4000


@dataclass(frozen=True)
class PublishOutcome:
    ok: bool
    output: str


class PublishRunner(Protocol):
    def __call__(
        self, repo_root: Path, branch: str, pr_number: int, *, is_draft: bool
    ) -> PublishOutcome: ...


def publish_pr(
    repo_root: Path, branch: str, pr_number: int, *, is_draft: bool
) -> PublishOutcome:
    """Run the branch compression command, then mark a draft PR ready."""
    worktree = worktree_paths(root=repo_root).get(branch)
    if worktree is None:
        return PublishOutcome(False, f"No worktree found for branch '{branch}'.")

    executable = shutil.which("toolbelt") or sys.argv[0]
    outcome = _run([executable, "git", "compress"], cwd=worktree)
    if not outcome.ok or not is_draft:
        return outcome
    return _run(["gh", "pr", "ready", str(pr_number)], cwd=worktree)


def _run(args: list[str], *, cwd: Path) -> PublishOutcome:
    try:
        result = logged_process.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as error:  # noqa: BLE001 - reported to the page
        return PublishOutcome(False, f"{type(error).__name__}: {error}")
    output = (result.stdout + result.stderr).strip()
    if result.returncode:
        return PublishOutcome(False, output[-OUTPUT_LIMIT:])
    return PublishOutcome(True, output[-OUTPUT_LIMIT:])
