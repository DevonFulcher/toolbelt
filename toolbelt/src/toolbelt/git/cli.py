import subprocess
from pathlib import Path
from typing import Optional, List

import typer
from typing_extensions import Annotated

from toolbelt.git.commands import git_pr, get_branch_name, git_safe_pull, git_fix, git_setup, is_git_repo, git_save
from toolbelt.env_var import get_env_var_or_exit, get_git_projects_workdir

import os
import re
import subprocess
from pathlib import Path

git_typer = typer.Typer(help="Git workflow commands")

@git_typer.command()
def pr(
    skip_tests: Annotated[bool, typer.Option("--skip-tests", help="Skip tests")] = False,
):
    """Create or view a pull request"""
    git_pr(skip_tests)

@git_typer.command()
def get(
    repo_url: Annotated[str, typer.Argument(help="URL of the repository to get")],
    service_name: Annotated[
        Optional[str],
        typer.Option(
            help="The name of the service for retrieving helm values. If not provided, the repo name will be used."
        ),
    ] = None,
):
    """Get a repository"""
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
    # TODO: This can use the `edit` shell function
    editor = get_env_var_or_exit("EDITOR")
    subprocess.run([editor, str(clone_path)], check=True)

@git_typer.command()
def save(
    message: Annotated[str, typer.Option("-m", "--message", help="Commit message")],
    no_verify: Annotated[bool, typer.Option(help="Skip pre-commit hooks")] = False,
    no_sync: Annotated[bool, typer.Option(help="Skip syncing changes in this stack")] = False,
    amend: Annotated[bool, typer.Option(help="Amend the last commit")] = False,
    pathspec: Annotated[List[str] | None, typer.Argument(help="Files to stage (defaults to '-A')")] = None,
):
    """Add, commit, and push changes"""
    git_save(message, no_verify, no_sync, amend, pathspec)

@git_typer.command()
def send(
    message: Annotated[str, typer.Option("-m", "--message", help="Commit/branch message")],
    no_verify: Annotated[bool, typer.Option(help="Skip pre-commit hooks")] = False,
    skip_tests: Annotated[bool, typer.Option(help="Skip tests")] = False,
    no_sync: Annotated[bool, typer.Option(help="Skip syncing changes in this stack")] = False,
    pathspec: Annotated[List[str] | None, typer.Argument(help="Files to stage (defaults to '-A')")] = None,
):
    """Save changes and create PR"""
    git_save(message, no_verify, no_sync, False, pathspec)
    git_pr(skip_tests)

@git_typer.command()
def change(
    branch: Annotated[Optional[str], typer.Argument(help="Branch name to change to. If omitted, will use fzf to select")] = None,
    new_branch: Annotated[Optional[str], typer.Option("-b", help="Create a new branch and switch to it")] = None,
):
    """Change git branch"""
    if new_branch:
        subprocess.run(["git", "checkout", "-b", new_branch], check=True)
    else:
        subprocess.run(["git", "checkout", get_branch_name(branch, "change")], check=True)
        git_safe_pull()

@git_typer.command()
def compare(
    compare_args: Annotated[List[str] | None, typer.Argument(help="Commands to pass to git diff")] = None,
):
    """Compare commits with git diff"""
    # Exclude files from diff that I rarely care about. Reference: https://stackoverflow.com/a/48259275/8925314
    subprocess.run(
        ["git", "diff", "--ignore-all-space"] # Ignore all whitespace differences
        + (compare_args or [])
        + [
            "--",
            ":!*Cargo.lock",
            ":!*poetry.lock",
            ":!*package-lock.json",
            ":!*pnpm-lock.yaml",
            ":!*uv.lock",
            ":!*go.sum",
        ]
    )

@git_typer.command()
def combine(
    branch: Annotated[str, typer.Argument(help="Branch to combine")],
):
    subprocess.run(["git", "merge", get_branch_name(branch)], check=True)

@git_typer.command()
def setup(
    repo_path: Annotated[str, typer.Argument(help="Path to the repository to setup")],
    service_name: Annotated[
        Optional[str],
        typer.Option(
            help="The name of the service for retrieving helm values. If not provided, the repo name will be used."
        ),
    ] = None,
):
    git_projects_workdir = get_git_projects_workdir()
    git_setup(
        target_path=Path(repo_path),
        git_projects_workdir=git_projects_workdir,
        service_name=service_name,
    )

@git_typer.command(name="safe-pull")
def safe_pull():
    """Safe pull"""
    git_safe_pull()

@git_typer.command()
def fix(
    message: Annotated[
        Optional[str],
        typer.Option("-m", "--message", help="Commit message to replace original commit message"),
    ] = None,
):
    """Fix the last commit by replacing it with the current changes"""
    git_fix(message)

@git_typer.command()
def list():
    """List all repos"""
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