import os
import re
import subprocess
from pathlib import Path
from typing import Annotated, Optional

import typer

from toolbelt.editor import open_in_editor
from toolbelt.env_var import get_git_projects_workdir
from toolbelt.git.commands import (
    get_branch_name,
    get_current_repo_root_path,
    git_branch_clean,
    git_pr,
    git_safe_pull,
    git_save,
    git_setup,
    is_git_repo,
    sync_repo,
    update_repo,
)
from toolbelt.git.worktrees import worktrees_typer

git_typer = typer.Typer(help="Git workflow commands")
git_typer.add_typer(worktrees_typer, name="worktree")
git_typer.add_typer(worktrees_typer, name="wt")  # Add alias for worktrees


@git_typer.command(
    help="Create a PR from the current branch or open the PR if one already exists."
)
def pr(
    skip_tests: Annotated[
        bool, typer.Option("--skip-tests", help="Skip tests")
    ] = False,
):
    git_pr(skip_tests)


@git_typer.command(
    name="branch-clean",
    help="Delete local branches whose upstream has been removed.",
)
def branch_clean():
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


@git_typer.command(help="Git add, commit, and push changes")
def save(
    message: Annotated[
        str | None, typer.Option("-m", "--message", help="Commit message")
    ] = None,
    no_verify: Annotated[bool, typer.Option(help="Skip pre-commit hooks")] = False,
    no_sync: Annotated[
        bool, typer.Option(help="Skip syncing changes in this stack")
    ] = False,
    amend: Annotated[bool, typer.Option(help="Amend the last commit")] = False,
    pathspec: Annotated[
        list[str] | None, typer.Argument(help="Files to stage (defaults to '-A')")
    ] = None,
):
    """Add, commit, and push changes"""
    git_save(message, no_verify, no_sync, amend, pathspec)


@git_typer.command(help="Save changes and create a PR")
def send(
    message: Annotated[
        str, typer.Option("-m", "--message", help="Commit/branch message")
    ],
    no_verify: Annotated[bool, typer.Option(help="Skip pre-commit hooks")] = False,
    skip_tests: Annotated[bool, typer.Option(help="Skip tests")] = False,
    no_sync: Annotated[
        bool, typer.Option(help="Skip syncing changes in this stack")
    ] = False,
    pathspec: Annotated[
        list[str] | None, typer.Argument(help="Files to stage (defaults to '-A')")
    ] = None,
):
    """Save changes and create PR"""
    git_save(message, no_verify, no_sync, False, pathspec)
    git_pr(skip_tests)


@git_typer.command(help="Change git branch and safe pull changes")
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
    if new_branch:
        subprocess.run(["git", "checkout", "-b", new_branch], check=True)
    else:
        subprocess.run(
            ["git", "checkout", get_branch_name(branch, "change")], check=True
        )
        git_safe_pull()
        update_repo(get_current_repo_root_path())


@git_typer.command(help="Compare commits with git diff")
def compare(
    compare_args: Annotated[
        list[str] | None, typer.Argument(help="Commands to pass to git diff")
    ] = None,
):
    # Exclude files from diff that I rarely care about. Reference: https://stackoverflow.com/a/48259275/8925314
    subprocess.run(
        ["git", "diff", "--ignore-all-space"]  # Ignore all whitespace differences
        + (compare_args or [])
        + [
            "--",
            ":!*Cargo.lock",
            ":!*poetry.lock",
            ":!*package-lock.json",
            ":!*pnpm-lock.yaml",
            ":!*uv.lock",
            ":!*go.sum",
        ],
        check=False,
    )


@git_typer.command(help="Merge changes from a branch into the current branch")
def combine(
    branch: Annotated[str, typer.Argument(help="Branch to combine")],
):
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
    git_projects_workdir = get_git_projects_workdir()
    git_setup(
        target_path=Path(repo_path),
        git_projects_workdir=git_projects_workdir,
        service_name=service_name,
    )


@git_typer.command(name="safe-pull", help="Pull changes except for merge conflicts")
def safe_pull():
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


@git_typer.command(help="Sync the current repo")
def sync():
    sync_repo()
