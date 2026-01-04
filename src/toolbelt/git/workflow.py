import os
import re
import subprocess
from pathlib import Path

import typer

from toolbelt.git.commits import store_commit
from toolbelt.logger import logger
from toolbelt.repos import current_repo, current_repo_name

from .branches import (
    get_current_branch_name,
    get_default_branch,
    get_parent_branch_name,
)
from .repo import current_repo_org, get_current_repo_root_path
from .worktrees_ops import delete_branch_and_worktree


def update_repo(target_path: Path):
    if (target_path / ".tool-versions").exists():
        # This may fail if the plugins in .tool-versions are not installed
        subprocess.run(["asdf", "install"], check=True)
    if (target_path / "uv.lock").exists():
        subprocess.run(["uv", "sync"], check=True)


def sync_repo() -> None:
    # Run git-town continue in case a git conflict happened during the last save
    subprocess.run(["git-town", "continue"], check=True)
    subprocess.run(["git-town", "sync", "--stack"], check=True)
    update_repo(get_current_repo_root_path())
    subprocess.run(["git", "push"], check=True)


def git_pr(skip_tests: bool) -> None:
    view_pr = subprocess.run(["gh", "pr", "view", "--web"], check=False)
    if view_pr.returncode == 0:
        return
    repo = current_repo()
    if repo and not skip_tests:
        repo.check()
    subprocess.run(
        ["gh", "pr", "create", "--web"],
        check=False,
    )


def git_branch_clean() -> None:
    """
    Delete local branches whose upstream has been removed.
    """
    subprocess.run(["git", "fetch", "-p"], check=True)
    branch_list = subprocess.run(
        ["git", "branch", "-vv"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    repo_root = get_current_repo_root_path()
    deleted_branches: list[str] = []
    for line in branch_list:
        if ": gone]" not in line:
            continue
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] in {"*", "+"}:
            if len(tokens) < 2:
                continue
            branch_name = tokens[1]
        else:
            branch_name = tokens[0]
        delete_branch_and_worktree(branch_name, repo_root=repo_root)
        deleted_branches.append(branch_name)

    if deleted_branches:
        logger.info("Deleted branches:")
        for branch_name in deleted_branches:
            logger.info(f"  {branch_name}")
    else:
        logger.info("No branches to delete.")


def check_for_parent_branch_merge_conflicts(
    *, current_branch: str, default_branch: str
) -> None:
    logger.info("Checking for merge conflicts with parent branch")
    try:
        parent_branch = get_parent_branch_name(current_branch)
    except subprocess.CalledProcessError:
        if current_branch == default_branch:
            # Default branch without upstream - this is unusual but can happen
            logger.warning(f"Warning: {default_branch} has no upstream branch set")
            raise typer.Exit(1)
        else:
            # Regular branch without upstream - offer to set it
            logger.info(f"Branch '{current_branch}' has no upstream branch set")
            should_set_upstream = input(
                "Would you like to set an upstream branch? (y/n): "
            )
            if should_set_upstream.lower() == "y":
                try:
                    # Try to set upstream to origin/branch_name
                    subprocess.run(
                        [
                            "git",
                            "branch",
                            "--set-upstream-to",
                            f"origin/{current_branch}",
                            current_branch,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    logger.info(f"Set upstream branch to origin/{current_branch}")
                    # Try conflict check again with newly set upstream
                    parent_branch = get_parent_branch_name(current_branch)
                except subprocess.CalledProcessError:
                    # If setting upstream failed (e.g., remote branch doesn't exist)
                    logger.warning(
                        "Failed to set upstream branch - remote branch may not exist"
                    )
                    should_push = input(
                        "Would you like to push and set upstream now? (y/n): "
                    )
                    if should_push.lower() == "y":
                        try:
                            subprocess.run(
                                [
                                    "git",
                                    "push",
                                    "--set-upstream",
                                    "origin",
                                    current_branch,
                                ],
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                            logger.info(
                                f"Pushed and set upstream to origin/{current_branch}"
                            )
                            # Try conflict check one final time
                            parent_branch = get_parent_branch_name(current_branch)
                        except subprocess.CalledProcessError:
                            logger.error("Failed to push and set upstream branch")
                            raise typer.Exit(1)
                    else:
                        raise typer.Exit(1)
            else:
                raise typer.Exit(1)

    if parent_branch:
        try:
            merge_tree_result = subprocess.run(
                ["git", "merge-tree", parent_branch, current_branch],
                capture_output=True,
                text=True,
                check=False,
            )
            if "changed in both" in merge_tree_result.stdout:
                logger.warning(
                    "⚠️  Warning: This commit may create merge conflicts with the parent branch."
                )
                proceed = input("Do you want to continue anyway? (y/n): ")
                if proceed.lower() != "y":
                    # Unstage changes if user aborts
                    subprocess.run(["git", "reset"], check=True)
                    logger.info("Changes unstaged. Aborting commit.")
                    raise typer.Exit(1)
        except subprocess.CalledProcessError:
            # This might happen in detached HEAD state
            logger.error(
                "Error checking for merge conflicts - you may be in detached HEAD state"
            )
            logger.error("Aborting to be safe")
            subprocess.run(["git", "reset"], check=True)
            raise typer.Exit(1)
    logger.info("No merge conflicts found with parent branch")


def git_save(
    message: str | None,
    no_verify: bool,
    no_sync: bool,
    amend: bool,
    pathspec: list[str] | None,
) -> None:
    if not message and not amend:
        raise typer.BadParameter("Commit message or --amend is required")
    current_branch = get_current_branch_name()
    default_branch = get_default_branch()

    # Check if the current branch is a default branch
    current_org = os.getenv("CURRENT_ORG")
    if current_org:
        remote_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # Extract org from GitHub URL (handles both HTTPS and SSH formats)
        org_match = re.search(r"[:/]([^/]+)/[^/]+$", remote_url)
        if (
            org_match
            and org_match.group(1) == current_org.replace("_", "-")
            and current_branch == default_branch
        ):
            assert message, "Message is required when committing to a default branch"
            new_branch_name = "devon/" + message.replace(" ", "_")
            should_commit = input(
                "On a default branch. "
                + f"Commit to a new branch called {new_branch_name}? (y/n): "
            )
            if should_commit.lower() == "y":
                subprocess.run(
                    ["git-town", "append", new_branch_name],
                    check=True,
                )
            else:
                logger.info(
                    "Changes not committed. Use `git commit` to commit to a default branch."
                )
                raise typer.Exit(1)
        else:
            logger.info(
                "Not on the default branch. "
                + f"Continuing from this branch: {current_branch}"
            )

    # Add changes to the staging area
    git_add_command = ["git", "add"]
    if pathspec:
        git_add_command.extend(pathspec)
    else:
        git_add_command.append("-A")
    subprocess.run(git_add_command, check=True)

    # Check for conflicts with the parent branch
    check_for_parent_branch_merge_conflicts(
        current_branch=current_branch, default_branch=default_branch
    )

    # Commit the changes
    git_commit_command = ["git", "commit"]
    if message:
        git_commit_command.append("-m")
        git_commit_command.append(message)
    if amend:
        git_commit_command.append("--amend")
    if no_verify:
        git_commit_command.append("--no-verify")
    subprocess.run(
        git_commit_command,
        check=True,
        text=True,
    )

    # Sync the changes
    if not no_sync:
        sync_repo()

    if message:
        store_commit(message, current_repo_name(), current_repo_org(), current_branch)


def git_safe_pull() -> None:
    """
    Safely pull changes from remote by checking for uncommitted changes and
    ensuring the pull can be done without conflicts.
    """
    # Check for uncommitted changes first
    uncommitted_check = subprocess.run(
        ["git", "diff-index", "--quiet", "HEAD", "--"],
        capture_output=True,
        text=True,
        check=False,
    )
    if uncommitted_check.returncode != 0:
        logger.warning(
            "⚠️  Uncommitted changes found. Please commit or stash them before pulling."
        )
        raise typer.Exit(1)

    current_branch = get_current_branch_name()

    # Fetch latest changes
    logger.info("Fetching latest changes...")
    subprocess.run(["git", "fetch"], check=True)

    # Check if current branch has diverged from remote
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{current_branch}"],
            check=True,
            capture_output=True,
            text=True,
        )

        # If we get here, it's safe to pull
        logger.info("Branch can be fast-forwarded. Pulling changes...")
        subprocess.run(["git", "pull"], check=True)
        logger.info("Successfully pulled changes!")

    except subprocess.CalledProcessError:
        logger.warning(
            "⚠️  Warning: Local branch has diverged from remote. Pulling might cause conflicts."
        )
        raise typer.Exit(1)
