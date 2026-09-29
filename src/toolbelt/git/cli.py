import os
import re
import subprocess
from pathlib import Path
from typing import Annotated, Optional

import typer

from toolbelt.bootstrap.repo_setup import git_setup
from toolbelt.editor import open_in_editor
from toolbelt.env_var import get_git_projects_workdir
from toolbelt.git.branches import get_branch_name
from toolbelt.git.commands import is_git_repo
from toolbelt.git.repo import get_current_repo_root_path
from toolbelt.git.workflow import (
    git_branch_clean,
    git_merge,
    git_pr,
    git_safe_pull,
    git_save,
    sync_repo,
    update_repo,
)
from toolbelt.git.stack.cli import stack_typer

git_typer = typer.Typer(
    help="""Git workflow commands, built around stacked branches.

Each stacked branch is named devon/<name>, lives in its own worktree at
$GIT_PROJECTS_WORKDIR/wt/<repo>/<name>, and has a tracked parent branch
(git config toolbelt-stack.<branch>.parent). The stack's base (e.g. main) is
not tracked. PRs target the parent branch, not main.

Typical flow: `append <name>` to branch off the current branch into a new
worktree; `cd "$(toolbelt git switch devon/<name>)"`; edit; `save -m <msg>` to
commit, sync and push; `send -m <msg>` or `pr` to open the PR; `merge <pr>` to
land it. Run `sync` any time to pull parent changes down the stack, and `tree`
to see the stack with PR/CI status.

Commands that commit, push, force-push, or delete branches and worktrees say
so in their own --help: notably append, save, send, sync, merge, compress,
set-parent, remove and branch-clean."""
)
# Stack commands (append, compress, diff-parent, remove, set-parent, tree,
# switch) live directly under `git`, not a nested `git stack` group. Worktree
# lifecycle is entirely folded into these — there is no separate
# `git worktree`/`git wt` group.
git_typer.registered_commands.extend(stack_typer.registered_commands)


@git_typer.command()
def pr(
    skip_tests: Annotated[
        bool, typer.Option("--skip-tests", help="Skip tests")
    ] = False,
):
    """Open the current branch's PR in the browser, or start creating one.

    If a PR exists, opens it (`gh pr view --web`). Otherwise runs the repo's
    unit tests (unless --skip-tests; repos without configured tests skip this)
    and opens `gh pr create --web` with the base set to the branch's tracked
    stack parent, falling back to the default branch. The PR is finished in
    the browser. Does not commit or push; use `save` first.
    """
    git_pr(skip_tests)


@git_typer.command()
def merge(
    pr: Annotated[
        str,
        typer.Argument(help="PR number, URL, or branch to merge."),
    ],
):
    """Squash-merge a PR on GitHub, then sync the stack it belongs to.

    Runs `gh pr merge --squash PR`, then `sync` rooted at the merged branch's
    own worktree, so it works from any directory. That sync rebases the merged
    branch's children onto the next unmerged ancestor and force-pushes them,
    then force-removes the merged branch's worktree (discarding any
    uncommitted changes there) and deletes its local branch and stack entry.
    If the branch has no worktree, only `branch-clean` runs.
    """
    git_merge(pr)


@git_typer.command(name="branch-clean")
def branch_clean():
    """Delete local branches whose upstream is gone, with their worktrees.

    Runs `git fetch -p`, then for each local branch whose upstream was
    deleted: removes its worktree (fails if it has uncommitted changes) and
    force-deletes the branch (`git branch -D`). Also drops stack entries for
    branches that no longer exist, reparenting their children onto the gone
    branch's parent. `sync` runs this automatically.
    """
    git_branch_clean()


@git_typer.command(
    help="Clone a repo to the standard location, set it up with common "
    + "config, and open it in an editor"
)
def get(
    repo_url: Annotated[str, typer.Argument(help="URL of the repository to get")],
    service_name: Annotated[
        Optional[str],
        typer.Option(
            help="The name of the service for retrieving helm values. If not provided, "
            + "the repo name will be used"
        ),
    ] = None,
):
    git_projects_workdir = get_git_projects_workdir()
    repo_name = re.sub(
        r"\..*$",
        "",
        os.path.basename(repo_url),
    )
    clone_path = git_projects_workdir / repo_name
    if not is_git_repo(clone_path):
        subprocess.run(
            ["git", "clone", repo_url, str(clone_path)],
            check=True,
        )
        if not os.path.isdir(clone_path):
            raise ValueError(f"Failed to clone {repo_url} to {clone_path}")
    git_setup(
        target_path=clone_path,
        git_projects_workdir=git_projects_workdir,
        service_name=service_name,
    )
    open_in_editor(clone_path)


@git_typer.command()
def save(
    message: Annotated[
        str | None, typer.Option("-m", "--message", help="Commit message")
    ] = None,
    no_verify: Annotated[bool, typer.Option(help="Skip pre-commit hooks")] = False,
    no_sync: Annotated[
        bool, typer.Option(help="Skip syncing the stack (which also skips pushing)")
    ] = False,
    amend: Annotated[bool, typer.Option(help="Amend the last commit")] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "-y",
            "--yes",
            help=("Auto-accept prompts (e.g. auto create a branch)"),
        ),
    ] = False,
    pathspec: Annotated[
        list[str] | None, typer.Argument(help="Files to stage (defaults to '-A')")
    ] = None,
):
    """Stage and commit all changes, then sync and push the stack.

    Stages with `git add -A` (tracked and untracked files, including
    deletions, across the whole worktree), or only PATHSPEC if given.
    Requires -m unless --amend. Pre-commit hooks run unless --no-verify.

    Before committing, checks whether the branch conflicts with its stack
    parent (`git merge-tree`) and, if so, asks whether to continue; declining
    unstages everything (`git reset`) and exits. -y continues without asking.

    After committing, runs `sync` (see `sync --help`), which pushes every
    branch in the stack and removes branches whose PRs have landed. That is
    the only push: with --no-sync nothing is pushed. If the branch is not in a
    tracked stack, the commit is kept but the sync step fails.

    On the default branch of a repo in $CURRENT_ORG, it instead offers (-y
    accepts) to start a new stacked branch devon/<message> (spaces become _):
    it commits there, puts the main worktree back on the default branch,
    creates a worktree for the new branch, installs deps, opens an editor, and
    syncs from that worktree. In other repos it commits directly to the default branch.

    The commit message is also recorded in ~/.toolbelt/storage.db for
    `toolbelt standup`.
    """
    git_save(message, no_verify, no_sync, amend, pathspec, yes)


@git_typer.command()
def send(
    message: Annotated[
        str, typer.Option("-m", "--message", help="Commit/branch message")
    ],
    no_verify: Annotated[bool, typer.Option(help="Skip pre-commit hooks")] = False,
    skip_tests: Annotated[bool, typer.Option(help="Skip tests")] = False,
    no_sync: Annotated[
        bool, typer.Option(help="Skip syncing the stack (which also skips pushing)")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "-y",
            "--yes",
            help=("Auto-accept prompts (e.g. auto create a branch)"),
        ),
    ] = False,
    pathspec: Annotated[
        list[str] | None, typer.Argument(help="Files to stage (defaults to '-A')")
    ] = None,
):
    """Run `save`, then open a PR for the branch.

    Stages, commits and syncs exactly like `save` (see `save --help`: stages
    all changes by default, runs hooks unless --no-verify, pushes via sync
    unless --no-sync), except -m is required and there is no --amend. Then,
    from the branch's worktree (the new one, if `save` created a branch), does
    what `pr` does: opens the existing PR, or runs unit tests (unless
    --skip-tests) and opens `gh pr create --web` against the stack parent.
    """
    commit_root = git_save(message, no_verify, no_sync, False, pathspec, yes)
    git_pr(skip_tests, cwd=commit_root)


@git_typer.command()
def change(
    branch: Annotated[
        Optional[str],
        typer.Argument(
            help="Branch name to change to. If omitted, will use fzf to select"
        ),
    ] = None,
    new_branch: Annotated[
        Optional[str], typer.Option("-b", help="Create a new branch and switch to it")
    ] = None,
):
    """Check out a branch in this worktree, fast-forward pull it, refresh deps.

    Switches in place; no worktree is created. Without BRANCH, pick with fzf.
    main, master and current all mean the repo's default branch. After the
    checkout it runs `safe-pull` and then asdf install / uv sync. If BRANCH
    does not exist, it asks whether to create it here, stacked on the current
    branch. -b NAME only runs `git checkout -b NAME` (not tracked, no pull).

    In a stack worktree prefer `switch`: checking out another branch here
    leaves the previous branch without a worktree, and `sync` needs one per
    stacked branch.
    """
    if new_branch:
        subprocess.run(["git", "checkout", "-b", new_branch], check=True)
    else:
        subprocess.run(
            ["git", "checkout", get_branch_name(branch, "change")], check=True
        )
        git_safe_pull()
        update_repo(get_current_repo_root_path())


def _split_revisions_and_paths(
    args: list[str], *, cwd: Path
) -> tuple[list[str], list[str]]:
    """Partition `git diff` args into (revisions, paths).

    `compare` appends its own `--` with lock-file excludes, so a user path
    passed before that separator would be read as a revision (`bad revision`).
    Splitting here lets `compare HEAD~2` and `compare src/foo.py` both work.

    An explicit `--` is honored (everything after it is a path). Otherwise an
    arg is treated as a path when it exists on disk; anything else is a
    revision. Order is preserved within each bucket.
    """
    if "--" in args:
        sep = args.index("--")
        return args[:sep], args[sep + 1 :]
    revisions, paths = [], []
    for arg in args:
        if (cwd / arg).exists():
            paths.append(arg)
        else:
            revisions.append(arg)
    return revisions, paths


@git_typer.command(help="Compare commits with an AST-aware diff (difftastic)")
def compare(
    compare_args: Annotated[
        list[str] | None, typer.Argument(help="Revisions and/or paths for git diff")
    ] = None,
    line: Annotated[
        bool,
        typer.Option(
            "--line",
            "-l",
            help="Use a plain line diff (rendered by delta) instead of "
            + "difftastic. Reach for this on large or generated diffs, or when "
            + "you want patch-like output.",
        ),
    ] = False,
):
    # `compare` is a view-only command, so it defaults to difftastic's
    # AST-aware diff; --line falls back to git's configured pager (delta).
    # difft is installed alongside toolbelt by the dotfiles bootstrap, so it's
    # assumed present.
    git_config_args = [] if line else ["-c", "diff.external=difft"]
    revisions, paths = _split_revisions_and_paths(compare_args or [], cwd=Path.cwd())
    # Exclude files from diff that I rarely care about. Reference: https://stackoverflow.com/a/48259275/8925314
    subprocess.run(
        ["git"]
        + git_config_args
        + ["diff", "--ignore-all-space"]  # Ignore all whitespace differences
        + revisions
        + ["--"]
        + paths
        + [
            ":!*Cargo.lock",
            ":!*poetry.lock",
            ":!*package-lock.json",
            ":!*pnpm-lock.yaml",
            ":!*uv.lock",
            ":!*go.sum",
        ],
        check=False,
    )


@git_typer.command()
def combine(
    branch: Annotated[str, typer.Argument(help="Branch to combine")],
):
    """Merge BRANCH into the current branch (plain `git merge`, no push).

    main, master and current all mean the repo's default branch; `-` means
    the previously checked-out branch.
    """
    subprocess.run(["git", "merge", get_branch_name(branch)], check=True)


@git_typer.command(help="Set up a repository with common config")
def setup(
    repo_path: Annotated[str, typer.Argument(help="Path to the repository to setup")],
    service_name: Annotated[
        Optional[str],
        typer.Option(
            help="The name of the service for retrieving helm values. If not provided, "
            + "the repo name will be used."
        ),
    ] = None,
):
    git_setup(
        target_path=Path(repo_path),
        service_name=service_name,
    )


@git_typer.command(name="safe-pull")
def safe_pull():
    """Pull the current branch only if it can fast-forward.

    Exits without pulling if tracked files have uncommitted changes (untracked
    files are ignored), or if HEAD is not an ancestor of origin/<branch>
    (local commits not on the remote, or no remote branch).
    """
    git_safe_pull()


@git_typer.command(name="list", help="List all repos")
def git_list():
    git_projects_workdir = get_git_projects_workdir()
    subprocess.run(
        [
            "eza",
            "--classify",
            "--all",
            "--group-directories-first",
            "--long",
            "--git",
            "--git-repos",
            "--no-permissions",
            "--no-user",
            "--no-time",
            str(git_projects_workdir),
        ],
        check=True,
    )


@git_typer.command()
def sync():
    """Merge each parent into its child across the stack, push, clean up.

    Syncs every branch under the current branch's top-of-stack ancestor
    (siblings included), top of the stack downward, each inside its own
    worktree. The current branch must be a tracked stack branch (not the base
    branch), and every branch in the stack needs its own worktree.

    For each branch: merges its parent (origin/<base> for the top branch),
    then origin/<branch>, then runs `git push -u`. If a branch's PR has
    merged on GitHub, its children are instead rebased onto the nearest
    unmerged ancestor, reparented onto it, and force-pushed
    (--force-with-lease). The merged branch's worktree is then force-removed
    (discarding any uncommitted changes there), and its local branch and
    stack entry are deleted.

    Afterward runs `branch-clean`, then refreshes deps (asdf install / uv sync
    --all-groups) in this worktree, or the main one if this was removed.

    Never commits uncommitted changes. git refuses a merge that would
    overwrite a file with local edits, and refuses a rebase with any unstaged
    changes. On a conflict it stops with the merge or rebase in progress:
    resolve, `git add`, then rerun `sync`, which finishes the merge commit or
    runs `git rebase --continue` for you.
    """
    sync_repo()
