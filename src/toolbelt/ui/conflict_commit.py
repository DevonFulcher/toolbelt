"""The commit a branch's conflict comes from, for the task card.

Two sources, in this order:

* the branch's worktree is stopped on a conflict (``tt g sync`` left a rebase
  or merge in progress): the commit git is applying (``REBASE_HEAD``, with its
  place among the rebase's commits) or the tip being merged (``MERGE_HEAD``),
  as ``sync`` logs it;
* otherwise, a PR that conflicts with its base: the first commit of the PR, in
  order, whose changes already conflict with the base. Nothing is merged or
  checked out: ``git merge-tree`` tries the merge in memory, and a binary
  search over the PR's commits finds where it starts to fail. This is the
  commit a rebase onto the base would stop on.

Both read local git only (the PR's head must be in the local object store and
``origin/<base>`` is as fresh as the last fetch).
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from toolbelt import logged_process
from toolbelt.git.stack.sync import branch_being_rebased, read_conflict
from toolbelt.git.stack.worktree import worktree_paths

type ConflictSource = Literal["pr", "rebase", "merge"]

#: Longest list of conflicted files kept.
MAX_FILES = 10

_SUMMARY_RE = re.compile(r'^(?P<short>\S+) "(?P<subject>.*)"$')


@dataclass(frozen=True)
class ConflictCommit:
    sha: str
    subject: str
    #: Files that conflict (at most ``MAX_FILES``).
    files: tuple[str, ...]
    source: ConflictSource
    #: For a stopped rebase: which commit this is and how many it replays.
    step: int | None = None
    steps: int | None = None


def _git(root: Path, *args: str):
    return logged_process.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


def _merge_tree(root: Path, base: str, head: str) -> list[str] | None:
    """The files that conflict when ``head`` is merged into ``base``, or None
    when it merges cleanly. ``git merge-tree`` exits 1 on conflicts and lists
    their files after the tree id, up to the first blank line."""
    result = _git(root, "merge-tree", "--write-tree", "--name-only", base, head)
    if result.returncode == 0:
        return None
    if result.returncode != 1:
        raise RuntimeError(result.stderr.strip() or "git merge-tree failed")
    files = []
    for line in result.stdout.splitlines()[1:]:
        if not line:
            break
        files.append(line)
    return files


def _subject(root: Path, sha: str) -> str:
    return _git(root, "log", "-1", "--format=%s", sha).stdout.strip()


def find_pr_conflict(root: Path, base: str, head: str) -> ConflictCommit | None:
    """The first commit of ``base..head`` whose merge into ``base`` conflicts,
    or None when ``head`` merges cleanly."""
    if _merge_tree(root, base, head) is None:
        return None
    listed = _git(
        root, "rev-list", "--reverse", "--topo-order", f"{base}..{head}"
    ).stdout.split()
    if not listed:
        return None
    # The tip conflicts: find the earliest commit that already does.
    low, high = 0, len(listed) - 1
    while low < high:
        middle = (low + high) // 2
        if _merge_tree(root, base, listed[middle]) is None:
            low = middle + 1
        else:
            high = middle
    files = _merge_tree(root, base, listed[low]) or []
    return ConflictCommit(
        sha=listed[low],
        subject=_subject(root, listed[low]),
        files=tuple(files[:MAX_FILES]),
        source="pr",
    )


def find_stopped_conflict(worktree: Path) -> ConflictCommit | None:
    """The commit a rebase or merge stopped in ``worktree`` is stuck on, or
    None when nothing is stopped there."""
    details = read_conflict(worktree)
    if details.commit is None:
        return None
    short = _SUMMARY_RE.match(details.commit)
    if short is None:
        return None
    ref = "REBASE_HEAD" if details.rebasing else "MERGE_HEAD"
    sha = _git(worktree, "rev-parse", ref).stdout.strip() or short["short"]
    return ConflictCommit(
        sha=sha,
        subject=short["subject"],
        files=details.files[:MAX_FILES],
        source="rebase" if details.rebasing else "merge",
        step=details.step,
        steps=details.steps,
    )


def _worktree_of(root: Path, branch: str) -> Path | None:
    """The worktree ``branch`` is checked out in. A worktree stopped
    mid-rebase has a detached HEAD, so those are matched by the branch they are
    rebasing."""
    paths = worktree_paths(root=root)
    if branch in paths:
        return paths[branch]
    listed = _git(root, "worktree", "list", "--porcelain").stdout.splitlines()
    for line in listed:
        if line.startswith("worktree "):
            candidate = Path(line.removeprefix("worktree "))
            if branch_being_rebased(candidate) == branch:
                return candidate
    return None


#: PR conflicts already worked out, by (repo path, head, base): the result only
#: changes when either moves.
_PR_CONFLICTS: dict[tuple[Path, str, str], ConflictCommit | None] = {}


def conflict_for(
    root: Path,
    branch: str,
    *,
    base_ref: str | None,
    head_oid: str | None,
    pr_conflicts: bool,
) -> ConflictCommit | None:
    """The conflict commit to show for ``branch`` (checked out in a worktree of
    the repo at ``root``): what its worktree is stopped on if anything, else,
    for a PR that conflicts with its base, where that conflict starts."""
    worktree = _worktree_of(root, branch)
    if worktree is not None:
        stopped = find_stopped_conflict(worktree)
        if stopped is not None:
            return stopped
    if not pr_conflicts or base_ref is None or head_oid is None:
        return None
    base = _git(root, "rev-parse", "--verify", "--quiet", f"origin/{base_ref}")
    if (
        base.returncode != 0
        or _git(root, "cat-file", "-e", f"{head_oid}^{{commit}}").returncode != 0
    ):
        return None
    key = (root, head_oid, base.stdout.strip())
    if key not in _PR_CONFLICTS:
        _PR_CONFLICTS[key] = find_pr_conflict(root, key[2], head_oid)
    return _PR_CONFLICTS[key]
