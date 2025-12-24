import subprocess
from typing import Literal

import typer

from toolbelt.logger import logger

DefaultBranchName = Literal["main", "master", "current"]

DEFAULT_BRANCH_NAMES: tuple[DefaultBranchName, ...] = (
    "main",
    "master",
    "current",
)


def get_default_branch() -> DefaultBranchName:
    for branch_name in DEFAULT_BRANCH_NAMES:
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", branch_name],
                check=True,
                capture_output=True,
                text=True,
            )
            return branch_name
        except subprocess.CalledProcessError:
            continue

    formatted_names = ", ".join(f"'{name}'" for name in DEFAULT_BRANCH_NAMES)
    logger.error(
        f"None of the default branches {formatted_names} exist.",
    )
    raise typer.Exit(1)


def get_current_branch_name() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def get_parent_branch_name(child_branch_name: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", f"{child_branch_name}@{{u}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def get_branch_name(
    branch: str | None, command: Literal["change", "combine"] | None = None
) -> str:
    if not branch:
        # Interactive branch selection
        branches: str = subprocess.run(
            ["git", "branch"], check=True, capture_output=True, text=True
        ).stdout
        fzf = subprocess.Popen(
            ["fzf"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        selected_branch, _ = fzf.communicate(input=branches)
        branch_name = selected_branch.strip()
    elif branch in DEFAULT_BRANCH_NAMES:
        branch_name = get_default_branch()
    elif branch == "-":
        if command == "combine":
            branch_name = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "@{-1}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        else:
            branch_name = branch
    elif command == "change":
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                check=True,
                capture_output=True,
                text=True,
            )
            branch_name = branch
        except subprocess.CalledProcessError:
            should_create_branch = input(
                f"Branch '{branch}' does not exist. Would you like to create it? (y/n): "
            )
            if should_create_branch.lower() == "y":
                subprocess.run(
                    ["git-town", "append", branch],
                    check=True,
                )
                branch_name = branch
            else:
                logger.error("Exiting. Unable to continue without a valid branch name.")
                raise typer.Exit(1)
    else:
        branch_name = branch
    return branch_name
