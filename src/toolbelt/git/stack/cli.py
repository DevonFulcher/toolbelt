"""Stack workflow commands, registered directly under `toolbelt git`.

Defines `append` (create a stacked branch + worktree), the read/navigation
commands (`tree`, `switch`), and the branch-level operations (`compress`,
`diff-parent`, `set-parent`). These are flattened onto the top-level `git`
group (see git/cli.py) rather than a nested `git stack` group. The same
primitives back `git save`/`git sync`.
"""

import subprocess

import typer

from toolbelt.editor import open_in_editor
from toolbelt.git.exec import run
from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.forge import GhForge
from toolbelt.git.stack.ops import (
    compress_branch,
    diff_parent_command,
    set_branch_parent,
)
from toolbelt.git.stack.sync import sync_stack
from toolbelt.git.stack.viz import render
from toolbelt.git.stack.worktree import worktree_paths
from toolbelt.git.workflow import update_repo
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
    # Copy dotfiles, then install deps so the worktree is runnable.
    copy_dotfiles(root=root, wt_path=wt_path)
    update_repo(wt_path)
    logger.info(f"Created worktree at {wt_path}")
    open_in_editor(wt_path)


@stack_typer.command()
def compress(
    message: str | None = typer.Option(
        None, "-m", "--message", help="Message for the squashed commit"
    ),
) -> None:
    """Squash the current branch's commits into one (force-pushes the branch)."""
    compress_branch(root=repo_root(), message=message)


@stack_typer.command(name="diff-parent")
def diff_parent(
    args: list[str] | None = typer.Argument(
        None, help="Extra arguments passed through to `git diff`"
    ),
    line: bool = typer.Option(
        False,
        "--line",
        "-l",
        help="Use a plain line diff (rendered by delta) instead of difftastic.",
    ),
) -> None:
    """Diff the current branch against its stack parent (AST-aware by default)."""
    root = repo_root()
    run(
        diff_parent_command(root=root, extra_args=args, line=line),
        cwd=root,
        check=False,
    )


@stack_typer.command(name="set-parent")
def set_parent(
    new_parent: str = typer.Argument(..., help="Branch to set as the new parent"),
) -> None:
    """Repoint the current branch's parent, then sync so it reconciles onto it."""
    root = repo_root()
    set_branch_parent(root=root, new_parent=new_parent)
    sync_stack(root=root, forge=GhForge(root))


@stack_typer.command()
def tree() -> None:
    """Print the stack tree."""
    root = repo_root()
    parents = lineage.all_parents(root=root)
    if not parents:
        logger.info("No tracked stacks. Use `git append <name>` to start one.")
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
