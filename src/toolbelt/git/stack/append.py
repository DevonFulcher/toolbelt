"""Create a new stacked branch and its worktree.

The core (`create_stacked_branch`) does only git + lineage and lands the
worktree at an explicitly supplied path, so it is fully testable against a
throwaway repo. The CLI layer derives the path from the user's environment and
runs the heavier worktree setup / editor open.
"""

from pathlib import Path

import typer

from toolbelt.git.exec import run
from toolbelt.git.stack.lineage import get_parent, set_parent
from toolbelt.git.worktrees import (
    _branch_name_for_worktree_name,
    _commit_uncommitted,
    current_branch,
)
from toolbelt.logger import logger


def _is_ancestor(ancestor: str, descendant: str, *, root: Path) -> bool:
    result = run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        check=False,
    )
    return result.returncode == 0


def _bring_up_to_date(parent: str, *, is_stack_base: bool, root: Path) -> None:
    """Move the checked-out ``parent`` to include everything on ``origin/<parent>``.

    A branch that is behind its remote is fast-forwarded. A stack base (no
    recorded parent) that has diverged, because it carries local commits such
    as the checkpoint, gets ``origin/<parent>`` merged in; a conflict aborts
    that merge, leaving the worktree clean, and stops with an error. A diverged
    stacked branch is left alone and stops with an error, since reconciling it
    is a decision for its owner (`toolbelt git sync` or a manual merge).
    """
    remote = f"origin/{parent}"
    remote_ref = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if remote_ref.returncode != 0 or _is_ancestor(remote, parent, root=root):
        return
    if _is_ancestor(parent, remote, root=root):
        run(["git", "merge", "--ff-only", remote], cwd=root, exit_on_error=True)
        return
    if not is_stack_base:
        logger.error(
            f"Error: {parent} has diverged from {remote}; reconcile them "
            f"(for example merge {remote} into it) and run append again."
        )
        raise typer.Exit(1)
    merge = run(["git", "merge", "--no-edit", remote], cwd=root, check=False)
    if merge.returncode != 0:
        run(["git", "merge", "--abort"], cwd=root, exit_on_error=True)
        logger.error(
            f"Error: merging {remote} into {parent} conflicts; the merge was "
            f"aborted and {parent} is unchanged. Resolve it by hand and run "
            f"append again."
        )
        raise typer.Exit(1)


def create_stacked_branch(
    name: str,
    *,
    root: Path,
    wt_path: Path,
    task: str | None = None,
    task_tracker: str | None = None,
    agent_link: str | None = None,
) -> str:
    """Create a child branch off the current branch and a worktree for it.

    Any uncommitted work in ``root`` is committed onto the *current* branch
    first (a WIP checkpoint), so a live process still working in ``root``
    keeps its exact in-flight state — just committed rather than dirty —
    instead of having it moved out from under it onto the new branch. Then
    ``origin`` is fetched and the current branch is brought up to date with its
    remote branch (see ``_bring_up_to_date``), so the new branch forks from the
    latest commit of that branch (of the base, when forking from the stack
    base) and logs which branch and commit that is. Records the new branch's
    parent (and ``task`` / ``task_tracker`` / ``agent_link``, when given) in
    lineage. Returns the new branch name. Does not run repo setup
    or open an editor.
    """
    if wt_path.exists():
        logger.error(f"Error: worktree path already exists: {wt_path}")
        raise typer.Exit(1)

    parent = current_branch(root)
    new_branch = _branch_name_for_worktree_name(name)

    # Checkpoint any uncommitted work onto the current branch before
    # branching, so `root` never ends up in a different state than whatever
    # was already running against it.
    _commit_uncommitted(root=root)
    run(["git", "fetch", "origin"], cwd=root, exit_on_error=True)
    _bring_up_to_date(
        parent, is_stack_base=get_parent(parent, root=root) is None, root=root
    )
    fork_point = run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        exit_on_error=True,
        capture_output=True,
    ).stdout.strip()
    logger.info(f"Forking {new_branch} from {parent} at {fork_point}")
    run(["git", "checkout", "-b", new_branch], cwd=root, exit_on_error=True)
    set_parent(
        new_branch,
        parent,
        root=root,
        task=task,
        task_tracker=task_tracker,
        agent_link=agent_link,
    )
    run(["git", "checkout", parent], cwd=root, exit_on_error=True)
    run(
        ["git", "worktree", "add", str(wt_path), new_branch],
        cwd=root,
        exit_on_error=True,
    )
    return new_branch
