"""What git says about a branch an agent might work on: its head, when it was
last committed to, and whether its worktree is free to work in."""

import time
from collections.abc import Callable
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


@dataclass(frozen=True)
class RepoFacts:
    """What inspecting any branch of a repo needs to know about the repo."""

    #: The user's own email: the author whose commits count as work.
    email: str
    #: The default branch as a ref (see ``_default_branch_ref``).
    default_ref: str


class RepoFactsCache:
    """``RepoFacts`` per repo, kept for ``ttl`` seconds so the branches of one
    dispatcher pass share one lookup instead of each repeating it. The facts
    are re-read on the next pass, so nothing is ever older than a few seconds."""

    def __init__(
        self, ttl: float = 5.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._facts: dict[Path, tuple[float, RepoFacts]] = {}

    def get(self, repo: Path) -> RepoFacts:
        now = self._clock()
        held = self._facts.get(repo)
        if held is not None and now - held[0] < self._ttl:
            return held[1]
        facts = RepoFacts(
            email=_git(repo, "config", "user.email"),
            default_ref=_default_branch_ref(repo),
        )
        self._facts[repo] = (now, facts)
        return facts


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


def _busy(worktree: Path) -> bool:
    """A merge, rebase, cherry-pick or revert is in progress in ``worktree``."""
    # One call for every marker: ``--git-path`` answers relative to the
    # worktree, or absolute.
    args = [arg for marker in _BUSY_MARKERS for arg in ("--git-path", marker)]
    paths = _git(worktree, "rev-parse", *args).splitlines()
    return any((worktree / path).exists() for path in paths)


def inspect_branch(
    repo: Path,
    branch: str,
    worktree: Path | None,
    facts: RepoFactsCache | None = None,
) -> BranchState:
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
    shared = facts.get(repo) if facts is not None else None
    email = shared.email if shared else _git(repo, "config", "user.email")
    default_ref = shared.default_ref if shared else _default_branch_ref(repo)
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
        default_ref,
    )
    if worktree is None:
        return BranchState(
            exists=True,
            head_sha=head,
            last_commit_at=datetime.fromisoformat(newest) if newest else None,
            clean=False,
            busy=False,
        )
    busy = _busy(worktree)
    return BranchState(
        exists=True,
        head_sha=head,
        last_commit_at=datetime.fromisoformat(newest) if newest else None,
        clean=_git(worktree, "status", "--porcelain") == "",
        busy=busy,
    )
