"""Maps claims to the tracked branches (tasks) they are about.

A ``worktree`` claim maps to the tracked branch checked out in that
worktree: resolved through ``git worktree list --porcelain`` of each
registered repo (worktrees normally live under
``$GIT_PROJECTS_WORKDIR/wt/<repo>/<name>``, but nothing here relies on that
layout). A ``ref`` claim maps by branch name, task ref (the stored ticket) or
task id. Claims that match nothing are unresolved: ignored by the UI and
listed by ``tt task status --verbose``.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from toolbelt import logged_process
from toolbelt.git.stack.store import StackStore
from toolbelt.task_claims.claim_file import Claim

#: A repo's checked-out worktrees: path -> branch (detached ones left out).
WorktreeLister = Callable[[Path], dict[Path, str]]


@dataclass(frozen=True)
class TrackedTask:
    """A tracked branch with what a claim may name it by."""

    repo_id: int
    repo_name: str
    branch: str
    task_id: str
    #: The stored ticket (``PLAT-412``), if any.
    task_ref: str | None
    #: Where the branch is checked out, if it is.
    worktree: Path | None

    @property
    def key(self) -> tuple[int, str]:
        return (self.repo_id, self.branch)


def _same_path(path: str) -> str:
    return os.path.realpath(path)


def git_worktrees(root: Path) -> dict[Path, str]:
    """``git worktree list --porcelain`` of the repo at ``root``. Empty when
    git cannot list them (the checkout moved or is no longer a repo)."""
    result = logged_process.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}
    worktrees: dict[Path, str] = {}
    current: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line.startswith("branch ") and current is not None:
            worktrees[current] = line.removeprefix("branch ").removeprefix(
                "refs/heads/"
            )
    return worktrees


def tracked_tasks(
    store: StackStore, list_worktrees: WorktreeLister
) -> list[TrackedTask]:
    """Every tracked branch of every registered repo. ``list_worktrees`` is
    called once per repo, so a caller that resolves many claims asks git once
    per read, not once per claim."""
    facts = store.tracked_branch_facts()
    tasks: list[TrackedTask] = []
    for repo in store.repos():
        checked_out: dict[str, Path] = {}
        if repo.path.is_dir():
            for path, branch in list_worktrees(repo.path).items():
                checked_out[branch] = path
        for fact in facts:
            if fact.repo_id != repo.id:
                continue
            tasks.append(
                TrackedTask(
                    repo_id=repo.id,
                    repo_name=repo.name,
                    branch=fact.name,
                    task_id=fact.task_id,
                    task_ref=fact.task,
                    worktree=checked_out.get(fact.name),
                )
            )
    return tasks


def resolve_claim(claim: Claim, tasks: list[TrackedTask]) -> list[TrackedTask]:
    """The tracked tasks ``claim`` is about (none when it matches nothing). A
    worktree names one branch; a ref may name several (the same branch name in
    two repos, or one ticket on a whole stack)."""
    if claim.kind == "worktree":
        wanted = _same_path(claim.path)
        return [
            task
            for task in tasks
            if task.worktree is not None and _same_path(str(task.worktree)) == wanted
        ]
    return tasks_named(claim.ref, tasks)


def tasks_named(ref: str, tasks: list[TrackedTask]) -> list[TrackedTask]:
    """The tracked tasks ``ref`` names: by branch name, task id or ticket."""
    folded = ref.casefold()
    return [
        task
        for task in tasks
        if task.branch == ref
        or task.task_id == ref
        or (task.task_ref is not None and task.task_ref.casefold() == folded)
    ]
