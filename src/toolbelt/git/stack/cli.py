"""Stack workflow commands, registered directly under `toolbelt git`.

Defines `append` (create a stacked branch + worktree), the read/navigation
commands (`tree`, `switch`), and the branch-level operations (`compress`,
`diff-parent`, `set-parent`, `remove`). These are flattened onto the
top-level `git` group (see git/cli.py) rather than a nested `git stack`
group. The same primitives back `git save`/`git sync`. Worktree lifecycle is
entirely folded in here — there is no separate `git worktree`/`git wt` group.
"""

import asyncio
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from rich.live import Live

from toolbelt import logged_process
from toolbelt.git.exec import run
from toolbelt.git.repo_lock import repo_lock
from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.forge import GhForge
from toolbelt.git.stack.ops import (
    compress_branch,
    diff_parent_command,
    push_if_fast_forward,
    retarget_open_pr,
    set_branch_parent,
)
from toolbelt.git.stack.status import BranchStatus, stream_branch_statuses
from toolbelt.git.stack.sync import sync_stack
from toolbelt.git.stack.viz import Columns, RepoTree, render, render_repos
from toolbelt.git.stack.worktree import worktree_paths
from toolbelt.git.workflow import update_repo
from toolbelt.git.worktrees import (
    _worktree_path_for_name,
    copy_dotfiles,
    current_branch,
    repo_root,
)
from toolbelt.git.worktrees_ops import delete_branch_and_worktree, main_worktree
from toolbelt.logger import logger
from toolbelt.tasks.config import configured_tracker, employer_orgs
from toolbelt.tasks.requirement import origin_owner, task_required

stack_typer = typer.Typer(help="Stack + worktree management")


@stack_typer.command()
def append(
    name: str = typer.Argument(
        ...,
        help="Name of the new stacked branch, without the devon/ prefix "
        "(it is added for you).",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Task this branch is for, as its key or URL in the configured "
        "tracker (tasks.tracker in the config file). Required for repos owned by "
        "an employer org (tasks.employer_orgs in the config file, default "
        "dbt-labs and fivetran); optional for any other repo, and on a "
        "personal laptop (IS_PERSONAL=true).",
    ),
    agent_link: str = typer.Option(
        ...,
        "--agent-link",
        help="Deep link to the Claude Desktop session working on this: "
        "claude://claude.ai/epitaxy/local_<session-uuid>. The current "
        "session's id is in $CLAUDE_CODE_HOST_SESSION_ID, so pass "
        "claude://claude.ai/epitaxy/$CLAUDE_CODE_HOST_SESSION_ID.",
    ),
) -> None:
    """Create a new branch stacked on the current one, in its own worktree.

    Any uncommitted changes here are committed onto the *current* branch as
    a checkpoint before branching, so this worktree keeps its exact
    in-flight state (now committed instead of dirty) — nothing gets moved
    out from under whatever is running here. The new branch forks from that
    checkpoint. Use this to continue in-flight work as a tracked stack
    member. There is no option for an untracked worktree.

    The worktree is created at $GIT_PROJECTS_WORKDIR/wt/<repo>/<NAME>, gets
    the repo root's dotfiles copied in (except .git and .venv), and has deps
    installed (asdf install / uv sync). Nothing is pushed, and your shell
    stays where it is; `cd` to the printed path.

    NAME is prefixed with "devon/" and normalized for use as a branch and
    directory name: "/" and spaces become "_". So pass a bare name — an
    already-prefixed "devon/foo" would become "devon/devon_foo".

    --task and --agent-link are stored as given with the branch; --task is
    also tagged with the configured task tracker's name (if any).

    --task is required only when the GitHub owner of `origin` is an employer
    org (tasks.employer_orgs in the config file, default dbt-labs and
    fivetran) or cannot be determined. Repos owned by anyone else are
    personal and may omit it, as may any repo with IS_PERSONAL=true. Without
    --task the branch has no task and no tracker is consulted.
    """
    root = repo_root()
    config_file = default_config_path()
    if task is None and task_required(root, config_file):
        orgs = ", ".join(employer_orgs(config_file))
        owner = origin_owner(root)
        why = (
            f"the repo's GitHub owner ({owner}) is an employer org"
            if owner
            else "the repo has no github.com `origin` remote to tell whose it is"
        )
        raise typer.BadParameter(
            f"--task is required: {why}. Repos whose owner is outside the "
            f"employer orgs ({orgs}; tasks.employer_orgs in the config file) "
            f"are personal and don't need a task. IS_PERSONAL=true also waives it.",
            param_hint="--task",
        )
    tracker = configured_tracker(config_file) if task is not None else None
    with repo_lock(root):
        wt_path = _worktree_path_for_name(name=name, repo_root=root)
        create_stacked_branch(
            name,
            root=root,
            wt_path=wt_path,
            task=task,
            task_tracker=tracker.name if tracker else None,
            agent_link=agent_link,
        )
        # Copy dotfiles, then install deps so the worktree is runnable.
        copy_dotfiles(root=root, wt_path=wt_path)
        update_repo(wt_path)
    logger.info(f"Created worktree at {wt_path}")


@stack_typer.command()
def compress(
    message: str | None = typer.Option(
        None, "-m", "--message", help="Message for the squashed commit"
    ),
) -> None:
    """Squash the current branch's commits into one, then force-push it.

    Soft-resets to the merge base with the stack parent and commits once
    (pre-commit hooks run), using -m or the branch's oldest commit subject.
    Anything already staged is folded into that commit; unstaged changes are
    left alone. Force-pushes (--force-with-lease) if the branch exists on
    origin. Child branches are untouched until the next `sync`.
    """
    root = repo_root()
    with repo_lock(root):
        compress_branch(root=root, message=message)


@stack_typer.command(name="diff-parent")
def diff_parent(
    args: Annotated[
        list[str] | None,
        typer.Argument(help="Extra arguments passed through to `git diff`"),
    ] = None,
    line: bool = typer.Option(
        False,
        "--line",
        "-l",
        help="Use a plain line diff (rendered by delta) instead of difftastic.",
    ),
) -> None:
    """Diff the current branch against its stack parent (AST-aware by default).

    Runs `git diff <parent>...HEAD`: only this branch's committed changes
    since it forked, not the parent's newer work or uncommitted edits.
    """
    root = repo_root()
    run(
        diff_parent_command(root=root, extra_args=args, line=line),
        cwd=root,
        check=False,
    )


@stack_typer.command(name="set-parent")
def set_parent(
    new_parent: str = typer.Argument(
        ..., help="Branch to set as the new parent (full name, e.g. devon/foo)"
    ),
) -> None:
    """Repoint the current branch's stack parent, then sync.

    Updates only the tracked parent (checking the branch exists and no cycle
    forms), then runs `sync` (see `sync --help`), which merges the new parent
    in and pushes. The new parent is also fast-forward pushed if origin already
    has it and it is ahead (never forced). It merges rather than rebases, so commits inherited from
    the old parent stay on the branch. Future `pr`/`send` target the new
    parent; an open PR on the branch is retargeted onto the new parent.
    """
    root = repo_root()
    forge = GhForge(root)
    with repo_lock(root):
        branch = set_branch_parent(root=root, new_parent=new_parent)
        sync_stack(root=root, forge=forge)
        # An untracked new parent is not part of the synced stack; keep an
        # already-published one current so the retargeted PR diffs cleanly.
        push_if_fast_forward(new_parent, root=root)
        retarget_open_pr(branch, new_parent, forge=forge)


def _repo_root_or_none() -> Path | None:
    result = logged_process.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def _known_repo_trees(cwd_root: Path | None) -> list[RepoTree]:
    """Every known repo with tracked stacks, marking the cwd repo's current
    branch. Repos whose checkout no longer exists are skipped."""
    cwd_common_dir = (
        lineage.repo_identity(cwd_root).git_common_dir if cwd_root else None
    )
    trees = []
    for repo, parents in lineage.known_repos():
        if not parents:
            continue
        if not repo.path.is_dir():
            logger.warning(f"Skipping {repo.name}: {repo.path} no longer exists.")
            continue
        is_cwd_repo = repo.git_common_dir == cwd_common_dir
        trees.append(
            RepoTree(
                name=repo.name,
                path=repo.path,
                parents=parents,
                current=current_branch(cwd_root) if cwd_root and is_cwd_repo else None,
                statuses={},
            )
        )
    return trees


@stack_typer.command()
def tree(
    next_action: Annotated[
        bool,
        typer.Option(
            "--next",
            help="Add what happens next on each branch (the `tt ui` Next column).",
        ),
    ] = False,
    lines: Annotated[
        bool,
        typer.Option(
            "--lines", help="Add the lines each PR adds and deletes (+added/-deleted)."
        ),
    ] = False,
    assignee: Annotated[
        bool,
        typer.Option(
            "--assignee",
            help="Add who each PR is assigned to, or else who a review is "
            "requested from.",
        ),
    ] = False,
) -> None:
    """Print every known repo's stack tree, with each branch's PR/CI/review status.

    Read-only.

    Each repo toolbelt tracks stacks for appears under its own `name (path)`
    header, with the current repo's current branch marked. The tree itself is
    local and renders instantly; the status column comes from one `gh pr view`
    per branch, run concurrently, and fills in as each completes. On a real
    terminal this redraws live; piped output waits for every lookup and prints
    once.

    --next adds what happens next on each branch (the `tt ui` Next column, for
    example "Publish PR in GitHub" for a draft that is ready), --lines the
    lines each PR adds and deletes, and --assignee who each PR is assigned to,
    or else who a review is requested from.
    """
    cwd_root = _repo_root_or_none()
    if cwd_root is not None:
        # Register (and import legacy config for) the current repo so it shows
        # up even if no other command has touched it yet.
        lineage.all_parents(root=cwd_root)
    columns = Columns(next=next_action, lines=lines, assignee=assignee)
    trees = [replace(t, columns=columns) for t in _known_repo_trees(cwd_root)]
    if not trees:
        logger.info("No tracked stacks. Use `git append <name>` to start one.")
        return

    # Per-repo statuses fill in as lookups complete (mutated in place).
    statuses: list[dict[str, BranchStatus]] = [{} for _ in trees]
    live: Live | None = None

    def view() -> str:
        return render_repos([replace(t, statuses=s) for t, s in zip(trees, statuses)])

    async def load_repo(index: int) -> None:
        repo_tree = trees[index]
        async for branch, status in stream_branch_statuses(
            sorted(repo_tree.parents), root=repo_tree.path
        ):
            statuses[index][branch] = status
            if live is not None:
                live.update(view(), refresh=True)

    async def load() -> None:
        await asyncio.gather(*(load_repo(i) for i in range(len(trees))))

    if sys.stdout.isatty():
        with Live(view(), auto_refresh=False) as live:
            asyncio.run(load())
    else:
        asyncio.run(load())
        typer.echo(view())


@stack_typer.command()
def switch(
    name: str | None = typer.Argument(
        None,
        help="Full branch name (e.g. devon/foo). If omitted, pick interactively.",
    ),
) -> None:
    """Print a stacked branch's worktree path (does not change directory).

    Use it as `cd "$(toolbelt git switch devon/<name>)"`. Without NAME it
    prints the stack tree and picks a branch with fzf; the tree also goes to
    stdout, so pass NAME when capturing the path.
    """
    root = repo_root()
    parents = lineage.all_parents(root=root)
    paths = worktree_paths(root=root)

    if name is None:
        if not parents:
            logger.error("No tracked stacks to switch between.")
            raise typer.Exit(1)
        # Show the tree for context, then pick a branch by name.
        typer.echo(render(parents, current=current_branch(root)))
        branches = sorted(parents.keys())
        try:
            proc = logged_process.run(
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
    typer.echo(str(target))


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
        help="Pass --force to git worktree remove (discards uncommitted changes).",
    ),
) -> None:
    """Remove a branch's worktree, force-delete the branch, drop it from the stack.

    Runs `git worktree remove`, which refuses a worktree with uncommitted or
    untracked files unless --force (which discards them), then
    `git branch -D`, which deletes the branch even if its commits are
    unmerged. The remote branch and any PR are left alone. Child branches are
    not reparented and still point at the removed branch, so run `set-parent`
    on them first.
    """
    root = repo_root()
    # `name` may be the branch checked out in `root` itself (the caller's own
    # current worktree): git can't remove a worktree with cwd pointed at the
    # worktree being removed, and if it's deleted out from under this
    # process's actual cwd, every subsequent git call needs a cwd that still
    # exists. So every op from here on uses the main worktree instead.
    main_wt = main_worktree(root)

    if name is None:
        parents = lineage.all_parents(root=root)
        if not parents:
            logger.error("No tracked stacks to remove from.")
            raise typer.Exit(1)
        typer.echo(render(parents, current=current_branch(root)))
        branches = sorted(parents.keys())
        try:
            proc = logged_process.run(
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
    with repo_lock(root):
        deleted_branch = delete_branch_and_worktree(
            name, repo_root=main_wt, force=force, caller_dirs=[root, Path.cwd()]
        )
        lineage.remove_parent(deleted_branch, root=main_wt)
