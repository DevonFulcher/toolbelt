"""`toolbelt git stack` command group.

Additive and namespaced: wiring this in does not touch the existing git-town
workflow. v1 exposes the read/navigation commands (`tree`, `switch`); `append`
and `sync` arrive in later phases.
"""

import subprocess

import typer

from toolbelt.editor import open_in_editor
from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.forge import GhForge
from toolbelt.git.stack.sync import sync_stack
from toolbelt.git.stack.viz import render
from toolbelt.git.stack.worktree import worktree_paths
from toolbelt.git.worktrees import (
    _worktree_path_for_name,
    copy_dotfiles,
    current_branch,
    repo_root,
)
from toolbelt.logger import logger

stack_typer = typer.Typer(help="Stack + worktree management")


@stack_typer.command()
def append(
    name: str = typer.Argument(..., help="Name of the new stacked branch"),
) -> None:
    """Create a new branch stacked on the current one, in its own worktree."""
    root = repo_root()
    wt_path = _worktree_path_for_name(name=name, repo_root=root)
    create_stacked_branch(name, root=root, wt_path=wt_path)
    # Copy dotfiles only — deliberately skip the heavier git_setup (which
    # chdirs, installs pre-commit, and shells out to the network) used by the
    # legacy worktree flow.
    copy_dotfiles(root=root, wt_path=wt_path)
    logger.info(f"Created worktree at {wt_path}")
    open_in_editor(wt_path)


@stack_typer.command()
def sync() -> None:
    """Merge-sync the whole stack the current branch belongs to."""
    root = repo_root()
    sync_stack(root=root, forge=GhForge(root))


@stack_typer.command()
def tree() -> None:
    """Print the stack tree."""
    root = repo_root()
    parents = lineage.all_parents(root=root)
    if not parents:
        logger.info("No tracked stacks. Use `stack append <name>` to start one.")
        return
    logger.info(render(parents, current=current_branch(root)))


@stack_typer.command()
def switch(
    name: str | None = typer.Argument(
        None,
        help="Branch to switch to. If omitted, pick interactively.",
    ),
) -> None:
    """Open a stacked branch's worktree, selecting from the stack tree."""
    root = repo_root()
    parents = lineage.all_parents(root=root)
    paths = worktree_paths(root=root)

    if name is None:
        if not parents:
            logger.error("No tracked stacks to switch between.")
            raise typer.Exit(1)
        # Show the tree for context, then pick a branch by name.
        logger.info(render(parents, current=current_branch(root)))
        branches = sorted(parents.keys())
        try:
            proc = subprocess.run(
                ["fzf"],
                input="\n".join(branches).encode(),
                capture_output=True,
                check=True,
            )
            name = proc.stdout.decode().strip()
        except subprocess.CalledProcessError as err:
            logger.error("No branch selected")
            raise typer.Exit(1) from err

    target = paths.get(name)
    if target is None:
        logger.error(f"No worktree found for branch '{name}'.")
        raise typer.Exit(1)
    open_in_editor(target)
