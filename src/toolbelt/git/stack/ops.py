"""Branch-level stack operations: compress, diff-parent, set-parent.

Each core takes an explicit ``root`` and does only git + lineage so it is
testable against a throwaway repo. The CLI layer resolves the current worktree
and (for set-parent) triggers a sync afterward.
"""

from pathlib import Path

import typer

from toolbelt.git.exec import run
from toolbelt.git.stack.lineage import all_parents, get_parent, set_parent
from toolbelt.git.worktrees import current_branch
from toolbelt.logger import logger


def _parent_or_exit(branch: str, *, root: Path) -> str:
    parent = get_parent(branch, root=root)
    if parent is None:
        logger.error(
            f"'{branch}' is not tracked in a stack. Start one with "
            "`git append <name>`."
        )
        raise typer.Exit(1)
    return parent


def _remote_branch_exists(branch: str, *, root: Path) -> bool:
    result = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _past_tips(branch: str, *, root: Path) -> list[str]:
    """Every commit ``branch`` has pointed at, per its reflog.

    Branch reflogs live in the common git dir, so every worktree sees the same
    entries. Empty when ``branch`` has no reflog or isn't a local ref.
    """
    result = run(
        ["git", "rev-list", "--walk-reflogs", branch],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    return list(dict.fromkeys(result.stdout.split()))


def _default_message(*, parent: str, base: str, root: Path) -> str:
    """The subject of the branch's oldest own commit.

    ``base..HEAD`` can still hold commits the parent has since rewritten away
    (e.g. squashed by ``sync``), because the branch forked from or merged them.
    Excluding everything reachable from any tip the parent has ever had, plus
    merge commits, leaves the branch's own commits. If that leaves nothing,
    falls back to the oldest subject in ``base..HEAD``.

    The parent's past tips go on the command line; branch reflogs expire and
    stay small, so this is well under the argument-length limit.
    """
    own = run(
        [
            "git",
            "log",
            "--reverse",
            "--no-merges",
            "--format=%s",
            f"{base}..HEAD",
            "--not",
            *_past_tips(parent, root=root),
        ],
        cwd=root,
        capture_output=True,
    ).stdout.splitlines()
    if own:
        return own[0].strip()
    return (
        run(
            ["git", "log", "--reverse", "--format=%s", f"{base}..HEAD"],
            cwd=root,
            capture_output=True,
        )
        .stdout.splitlines()[0]
        .strip()
    )


def compress_branch(
    *, root: Path, message: str | None = None, push: bool = True
) -> None:
    """Squash the current branch's own commits (those after its parent) into one.

    Rewrites history, so the branch's remote is force-pushed to match (unless
    ``push`` is False — ``sync`` sets this since it does its own push right
    after, for every branch, compressed or not). Children are left untouched
    — a later ``git sync`` reconciles them cleanly, since the squashed commit
    carries the same tree they already merged.
    """
    branch = current_branch(root)
    parent = _parent_or_exit(branch, root=root)

    base = run(
        ["git", "merge-base", parent, "HEAD"],
        cwd=root,
        capture_output=True,
    ).stdout.strip()

    count = int(
        run(
            ["git", "rev-list", "--count", f"{base}..HEAD"],
            cwd=root,
            capture_output=True,
        ).stdout.strip()
        or "0"
    )
    if count == 0:
        logger.info(
            f"'{branch}' has no commits beyond '{parent}'; nothing to compress."
        )
        return
    if count == 1 and message is None:
        logger.info(f"'{branch}' already has a single commit; nothing to compress.")
        return

    if message is None:
        # Default to the branch's first (oldest) commit subject, like git-town.
        message = _default_message(parent=parent, base=base, root=root)

    # Soft reset keeps the working tree and index, so the commit captures every
    # change since the fork point as one commit; unstaged work is left alone.
    run(["git", "reset", "--soft", base], cwd=root, exit_on_error=True)
    run(["git", "commit", "-m", message], cwd=root, exit_on_error=True)
    logger.info(f"Compressed {count} commits on '{branch}' into one.")

    if push and _remote_branch_exists(branch, root=root):
        run(
            ["git", "push", "--force-with-lease", "origin", branch],
            cwd=root,
            exit_on_error=True,
        )
        logger.info(f"Force-pushed '{branch}'.")


def diff_parent_command(
    *, root: Path, extra_args: list[str] | None = None, line: bool = False
) -> list[str]:
    """Build the ``git diff`` command for the current branch vs its stack parent.

    Uses the three-dot form (``parent...HEAD``) so the diff shows only this
    branch's own changes since it forked, not the parent's newer work.

    Defaults to difftastic's AST-aware diff (via ``diff.external``); pass
    ``line=True`` for a plain line diff rendered by the configured pager (delta).
    difft is installed alongside toolbelt by the dotfiles bootstrap, so it's
    assumed present.
    """
    branch = current_branch(root)
    parent = _parent_or_exit(branch, root=root)
    config_args = [] if line else ["-c", "diff.external=difft"]
    return ["git", *config_args, "diff", f"{parent}...HEAD", *(extra_args or [])]


def _would_create_cycle(*, branch: str, new_parent: str, root: Path) -> bool:
    """True if making ``new_parent`` the parent of ``branch`` forms a cycle.

    A cycle happens when ``branch`` is already an ancestor of ``new_parent`` in
    the lineage, so walk up from ``new_parent`` and see if we reach ``branch``.
    """
    parents = all_parents(root=root)
    node: str | None = new_parent
    seen: set[str] = set()
    while node is not None and node not in seen:
        if node == branch:
            return True
        seen.add(node)
        node = parents.get(node)
    return False


def set_branch_parent(*, root: Path, new_parent: str) -> str:
    """Repoint the current branch's stack parent to ``new_parent`` (lineage only).

    Validates that ``new_parent`` is a real branch and does not create a cycle.
    Returns the current branch name. Does not sync — the CLI runs the sync after
    so the branch is reconciled onto its new parent.
    """
    branch = current_branch(root)
    if new_parent == branch:
        logger.error("A branch cannot be its own parent.")
        raise typer.Exit(1)

    exists = run(
        ["git", "rev-parse", "--verify", "--quiet", new_parent],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if exists.returncode != 0:
        logger.error(f"Branch '{new_parent}' does not exist.")
        raise typer.Exit(1)

    if _would_create_cycle(branch=branch, new_parent=new_parent, root=root):
        logger.error(
            f"Setting '{new_parent}' as parent of '{branch}' would create a cycle."
        )
        raise typer.Exit(1)

    set_parent(branch, new_parent, root=root)
    logger.info(f"Set parent of '{branch}' to '{new_parent}'.")
    return branch
