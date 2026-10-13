"""What git says about a branch an agent might work on: its head, when it was
last committed to, and whether its worktree is free to work in."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from toolbelt.git.exec import run

#: Git's own markers of an operation in progress in a worktree.
_BUSY_MARKERS = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)


@dataclass(frozen=True)
class BranchState:
    #: There is a local branch with this name. Without one nothing else here
    #: is meaningful.
    exists: bool
    head_sha: str
    #: The newest non-merge commit of the user's own on the branch and not
    #: yet on the default branch (sync's merges say nothing about whether
    #: anyone is working on it); None if there is none.
    last_commit_at: datetime | None
    #: No uncommitted changes. Always False without a worktree.
    clean: bool
    #: A merge, rebase, cherry-pick or revert is in progress.
    busy: bool


def _git(root: Path, *args: str) -> str:
    return run(["git", *args], cwd=root, capture_output=True).stdout.strip()


def _default_branch_ref(repo: Path) -> str:
    """The repo's default branch as a ref: ``origin``'s HEAD when known, else
    the first of the usual names that exists."""
    result = run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    for name in ("origin/main", "origin/master", "main", "master"):
        found = run(
            ["git", "rev-parse", "--verify", "--quiet", name],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        if found.returncode == 0:
            return name
    raise RuntimeError(f"{repo} has no default branch to measure a branch against")


def inspect_branch(repo: Path, branch: str, worktree: Path | None) -> BranchState:
    found = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if found.returncode != 0:
        return BranchState(
            exists=False, head_sha="", last_commit_at=None, clean=False, busy=False
        )
    head = found.stdout.strip()
    email = _git(repo, "config", "user.email")
    # Only the branch's own commits: what is already on the default branch
    # (and came in through a merge) is not work on this branch.
    newest = _git(
        repo,
        "log",
        "-1",
        "--no-merges",
        f"--author={email}",
        "--format=%cI",
        f"refs/heads/{branch}",
        "--not",
        _default_branch_ref(repo),
    )
    if worktree is None:
        return BranchState(
            exists=True,
            head_sha=head,
            last_commit_at=datetime.fromisoformat(newest) if newest else None,
            clean=False,
            busy=False,
        )
    # ``--git-path`` answers relative to the worktree, or absolute.
    busy = any(
        (worktree / _git(worktree, "rev-parse", "--git-path", marker)).exists()
        for marker in _BUSY_MARKERS
    )
    return BranchState(
        exists=True,
        head_sha=head,
        last_commit_at=datetime.fromisoformat(newest) if newest else None,
        clean=_git(worktree, "status", "--porcelain") == "",
        busy=busy,
    )
