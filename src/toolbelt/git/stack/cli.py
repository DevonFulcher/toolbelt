"""Stack workflow commands, registered directly under `toolbelt git`.

Defines `append` (create a stacked branch + worktree), the read/navigation
commands (`tree`, `switch`), and the branch-level operations (`compress`,
`diff-parent`, `set-parent`, `remove`). These are flattened onto the
top-level `git` group (see git/cli.py) rather than a nested `git stack`
group. The same primitives back `git save`/`git sync`. Worktree lifecycle is
entirely folded in here — there is no separate `git worktree`/`git wt` group.
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
from toolbelt.git.worktrees_ops import delete_branch_and_worktree
from toolbelt.logger import logger

stack_typer = typer.Typer(help="Stack + worktree management")


@stack_typer.command()
def append(
    name: str = typer.Argument(
        ...,
        help="Name of the new stacked branch, without the devon/ prefix "
        "(it is added for you).",
    ),
) -> None:
    """Create a new branch stacked on the current one, in its own worktree.

    Any uncommitted changes here are committed onto the *current* branch as
    a checkpoint before branching, so this worktree keeps its exact
    in-flight state (now committed instead of dirty) — nothing gets moved
    out from under whatever is running here. The new branch forks from that
    checkpoint. Use this to continue in-flight work as a tracked stack
    member — it's the only way to create a worktree in this tool; there is
    no separate untracked option.

    NAME is prefixed with "devon/" and normalized for use as a branch and
    directory name: "/" and spaces become "_". So pass a bare name — an
    already-prefixed "devon/foo" would become "devon/devon_foo".
    """
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


@stack_typer.command()
def remove(
    name: str | None = typer.Argument(
        None,
        help="Branch name (with or without devon/ prefix). If omitted, pick interactively.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Pass --force to git worktree remove.",
    ),
) -> None:
    """Remove a branch's worktree, delete the branch, and drop it from the stack."""
    root = repo_root()

    if name is None:
        parents = lineage.all_parents(root=root)
        if not parents:
            logger.error("No tracked stacks to remove from.")
            raise typer.Exit(1)
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

    # delete_branch_and_worktree resolves the prefixed/bare form; use its
    # return value (not the raw argument) as the lineage key so a bare name
    # like "feature" still clears "devon/feature"'s parent entry.
    deleted_branch = delete_branch_and_worktree(name, repo_root=root, force=force)
    lineage.remove_parent(deleted_branch, root=root)
