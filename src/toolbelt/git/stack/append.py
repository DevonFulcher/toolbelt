"""Create a new stacked branch and its worktree.

The core (`create_stacked_branch`) does only git + lineage and lands the
worktree at an explicitly supplied path, so it is fully testable against a
throwaway repo. The CLI layer derives the path from the user's environment and
runs the heavier worktree setup / editor open.
"""

from pathlib import Path

import typer

from toolbelt.git.exec import run
from toolbelt.git.stack.lineage import set_parent
from toolbelt.git.worktrees import (
    _branch_name_for_worktree_name,
    _commit_uncommitted,
    current_branch,
)
from toolbelt.logger import logger


def create_stacked_branch(name: str, *, root: Path, wt_path: Path) -> str:
    """Create a child branch off the current branch and a worktree for it.

    Any uncommitted work in ``root`` is moved onto the new branch. Records the
    new branch's parent in lineage. Returns the new branch name. Does not run
    repo setup or open an editor.
    """
    if wt_path.exists():
        logger.error(f"Error: worktree path already exists: {wt_path}")
        raise typer.Exit(1)

    parent = current_branch(root)
    new_branch = _branch_name_for_worktree_name(name)

    # Create the child off the current branch, carrying any uncommitted work
    # onto it, then restore the original branch in this worktree.
    run(["git", "checkout", "-b", new_branch], cwd=root, exit_on_error=True)
    set_parent(new_branch, parent, root=root)
    _commit_uncommitted(root=root)
    run(["git", "checkout", parent], cwd=root, exit_on_error=True)
    run(
        ["git", "worktree", "add", str(wt_path), new_branch],
        cwd=root,
        exit_on_error=True,
    )
    return new_branch
