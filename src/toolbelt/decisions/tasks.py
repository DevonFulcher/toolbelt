"""Which tracked task a decision is about: named by the agent, or the task of
the worktree the session runs in."""

import os
from collections.abc import Callable
from pathlib import Path

from toolbelt import logged_process
from toolbelt.task_claims.resolution import TrackedTask, tasks_named


class NoTask(LookupError):
    """No tracked task matches."""


class AmbiguousTask(LookupError):
    """Several tracked tasks match: the agent has to say which."""


def worktree_root(cwd: Path) -> Path | None:
    """The top of the git worktree ``cwd`` is in (``None``: not in one)."""
    result = logged_process.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def resolve_task(
    ref: str | None,
    tasks: list[TrackedTask],
    cwd: Path,
    root_of: Callable[[Path], Path | None] = worktree_root,
) -> TrackedTask:
    """The task ``ref`` names (a branch, a ticket or a task id), or, without
    one, the task checked out in ``cwd``'s worktree."""
    if ref:
        found = tasks_named(ref, tasks)
        what = f"named {ref!r}"
    else:
        root = root_of(cwd)
        found = (
            [
                task
                for task in tasks
                if task.worktree is not None
                and os.path.realpath(task.worktree) == os.path.realpath(root)
            ]
            if root is not None
            else []
        )
        what = f"checked out in {cwd}"
    if not found:
        raise NoTask(f"No tracked task is {what}. Name the branch or ticket.")
    if len(found) > 1:
        options = ", ".join(f"{task.repo_name}:{task.branch}" for task in found)
        raise AmbiguousTask(f"Several tasks are {what}: {options}. Name the branch.")
    return found[0]
