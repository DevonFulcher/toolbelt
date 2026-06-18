"""Sync a stack: merge each parent into its child, restacking when one lands.

Day-to-day this merges (not rebases) root -> leaf: no force-push, and a conflict
is resolved with a single ``git commit`` + re-run (idempotent). When a parent's
PR has merged, its children are rebased onto the grandparent instead
("restack-on-land") and the landed branch is cleaned up — the only place we
force-push / rewrite history. Whether a parent landed is decided by the injected
`Forge`, so it is authoritative (a squash-merge can't be detected from local
ancestry) and testable.

Worktree-per-branch is assumed: each branch is synced inside its own worktree.
"""

from pathlib import Path

import typer

from toolbelt.git.exec import run
from toolbelt.git.stack.forge import Forge
from toolbelt.git.stack.lineage import all_parents, resolve_stack, set_parent
from toolbelt.git.stack.worktree import worktree_paths
from toolbelt.git.worktrees import current_branch
from toolbelt.git.worktrees_ops import delete_branch_and_worktree
from toolbelt.logger import logger


def _git_path(worktree: Path, name: str) -> Path | None:
    """Resolve a git metadata path (e.g. ``rebase-merge``) for ``worktree``."""
    result = run(
        ["git", "rev-parse", "--git-path", name],
        cwd=worktree,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = worktree / path
    return path


def _branch_being_rebased(worktree: Path) -> str | None:
    """The branch a mid-rebase worktree is rebasing, or None if not rebasing.

    During a rebase the worktree's HEAD is detached, so the branch name must be
    read from the rebase state (``rebase-merge``/``rebase-apply/head-name``).
    """
    for state_dir in ("rebase-merge", "rebase-apply"):
        head_name = _git_path(worktree, f"{state_dir}/head-name")
        if head_name is not None and head_name.exists():
            return head_name.read_text().strip().removeprefix("refs/heads/")
    return None


def _rebase_in_progress(worktree: Path) -> bool:
    """True if ``worktree`` is mid-rebase (a prior restack hit a conflict)."""
    for state_dir in ("rebase-merge", "rebase-apply"):
        path = _git_path(worktree, state_dir)
        if path is not None and path.exists():
            return True
    return False


def _restack(child: str, *, worktree: Path, onto: str, parent_tip: str) -> bool:
    """Rebase ``child``'s own commits onto ``onto``; resume if already mid-rebase.

    Returns True on success, False if a conflict left the rebase in progress.
    """
    if _rebase_in_progress(worktree):
        result = run(["git", "rebase", "--continue"], cwd=worktree, check=False)
    else:
        result = run(
            ["git", "rebase", "--onto", onto, parent_tip, child],
            cwd=worktree,
            check=False,
        )
    return result.returncode == 0


def sync_stack(*, root: Path, forge: Forge) -> None:
    """Merge-sync the whole stack the current branch belongs to.

    Processes branches root -> leaf so each parent is up to date before being
    merged into its children. The stack root merges ``origin/<base>`` so landed
    work propagates down. When ``forge`` reports a parent's PR merged, that
    parent's children are rebased onto the grandparent and the parent is removed.
    """
    run(["git", "fetch", "-p"], cwd=root, exit_on_error=True)

    # When resuming after a restack conflict, this worktree is mid-rebase and
    # its HEAD is detached; recover the branch from the rebase state.
    rebasing = _branch_being_rebased(root)
    branch = rebasing or current_branch(root)
    stack = resolve_stack(branch, root=root)
    parents = all_parents(root=root)
    tracked = set(parents.keys())
    paths = worktree_paths(root=root)
    # A mid-rebase worktree shows as detached in `git worktree list`, so map the
    # branch being rebased back to this worktree.
    if rebasing is not None:
        paths.setdefault(rebasing, root)

    # Authoritative, queried once per branch.
    landed = {b for b in stack if forge.pr_is_merged(b)}

    to_cleanup: list[str] = []
    for child in stack:
        if child in landed:
            # Its PR merged; don't sync it — it is removed once its children are
            # restacked off it. (Cascading lands in one pass are out of scope.)
            continue

        worktree = paths.get(child)
        if worktree is None:
            logger.error(
                f"No worktree found for '{child}'. Every stacked branch needs "
                "its own worktree (create it with `stack append`)."
            )
            raise typer.Exit(1)

        parent = parents[child]

        if parent in landed:
            # Restack onto the grandparent (the landed parent's parent).
            grandparent = parents[parent]
            onto = grandparent if grandparent in tracked else f"origin/{grandparent}"
            parent_tip = run(
                ["git", "rev-parse", parent],
                cwd=root,
                capture_output=True,
            ).stdout.strip()

            if not _restack(child, worktree=worktree, onto=onto, parent_tip=parent_tip):
                logger.error(
                    f"Rebase conflict restacking '{child}' onto '{grandparent}' "
                    f"in {worktree}. Resolve, `git add`, then re-run `stack sync`."
                )
                raise typer.Exit(1)

            set_parent(child, grandparent, root=root)
            run(
                ["git", "push", "--force-with-lease", "origin", child],
                cwd=worktree,
                exit_on_error=True,
            )
            if parent not in to_cleanup:
                to_cleanup.append(parent)
        else:
            # The base (e.g. main) has no lineage entry; pull it from the remote.
            merge_ref = parent if parent in tracked else f"origin/{parent}"
            result = run(
                ["git", "merge", "--no-edit", merge_ref],
                cwd=worktree,
                check=False,
            )
            if result.returncode != 0:
                logger.error(
                    f"Merge conflict while syncing '{child}' in {worktree}. "
                    "Resolve the conflict, commit, then re-run `stack sync`."
                )
                raise typer.Exit(1)
            run(
                ["git", "push", "-u", "origin", child],
                cwd=worktree,
                exit_on_error=True,
            )

    for parent in to_cleanup:
        parent_wt = paths.get(parent)
        if parent_wt is not None and parent_wt.resolve() == root.resolve():
            logger.warning(
                f"Skipping cleanup of landed '{parent}': it is the current "
                "worktree. Switch away and re-run `stack sync`."
            )
            continue
        delete_branch_and_worktree(parent, repo_root=root, force=True)
        logger.info(f"Cleaned up landed branch '{parent}'.")
