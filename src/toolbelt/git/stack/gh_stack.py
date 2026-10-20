"""Unlinking a PR from GitHub's native stacked PRs.

toolbelt does not link stacks on GitHub. A PR that sits in a GitHub stack
(linked with `gh stack link` by an older toolbelt, or by hand) cannot be
squash-merged normally, and GitHub's asynchronous stack merge rewrites the
children's remote branches while toolbelt restacks them. ``unlink_github_stack``
removes such a stack when the user asks; nothing calls it automatically.
"""

import itertools
import json

import typer

from toolbelt import logged_process
from toolbelt.git.exec import capture
from toolbelt.logger import logger


def github_stack_number(pr: str) -> int | None:
    """The number of the GitHub stack the PR is in, or ``None``.

    ``pr`` is a PR number, URL, or branch name, as `gh pr view` takes it.
    """
    number = json.loads(capture(["gh", "pr", "view", pr, "--json", "number"]))["number"]
    pages = json.loads(
        capture(["gh", "api", "--paginate", "--slurp", "repos/{owner}/{repo}/stacks"])
    )
    for stack in itertools.chain.from_iterable(pages):
        if any(pull["number"] == number for pull in stack["pull_requests"]):
            return stack["number"]
    return None


def unlink_github_stack(pr: str) -> None:
    """Remove the GitHub stack the PR is in, with `gh stack unstack`.

    This only dissolves the grouping on GitHub; PRs, branches and bases are
    untouched. GitHub keeps a stack whose PRs are queued for merge or have
    auto-merge enabled, which is reported as an error.
    """
    stack = github_stack_number(pr)
    if stack is None:
        logger.info(f"PR '{pr}' is not in a GitHub stack.")
        return
    logged_process.run(["gh", "stack", "unstack", str(stack)], check=True)
    if github_stack_number(pr) is not None:
        logger.error(
            f"GitHub kept stack #{stack}: some of its PRs are queued for merge or "
            "have auto-merge enabled."
        )
        raise typer.Exit(1)
    logger.info(f"Unlinked GitHub stack #{stack}.")
