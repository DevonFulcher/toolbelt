"""Sync a stack by merging each parent into its child, root -> leaf.

Merge (not rebase) keeps this simple: no force-push, no re-pointing children,
and a conflict is resolved with a single ``git commit`` + re-running ``sync``
(the operation is idempotent). Restack-on-land (rebasing children when a parent
PR merges) is layered on in a later phase via an injected forge.

Worktree-per-branch is assumed: each tracked branch is synced inside its own
worktree, so no branch checkout juggling is needed.
"""

from pathlib import Path

import typer

from toolbelt.git.exec import run
from toolbelt.git.stack.lineage import all_parents, resolve_stack
from toolbelt.git.stack.worktree import worktree_paths
from toolbelt.git.worktrees import current_branch
from toolbelt.logger import logger


def sync_stack(*, root: Path) -> None:
    """Merge-sync the entire stack the current branch belongs to.

    Processes branches root -> leaf so each parent is up to date before it is
    merged into its children. The stack root merges ``origin/<base>`` (e.g.
    ``origin/main``) so landed work propagates down the stack.
    """
    run(["git", "fetch", "-p"], cwd=root, exit_on_error=True)

    branch = current_branch(root)
    stack = resolve_stack(branch, root=root)
    parents = all_parents(root=root)
    tracked = set(parents.keys())
    paths = worktree_paths(root=root)

    for child in stack:
        worktree = paths.get(child)
        if worktree is None:
            logger.error(
                f"No worktree found for '{child}'. Every stacked branch needs "
                "its own worktree (create it with `stack append`)."
            )
            raise typer.Exit(1)

        parent = parents[child]
        # The base (e.g. main) has no lineage entry; pull it from the remote so
        # landed commits flow into the stack. Tracked parents are merged from
        # their freshly-synced local branch.
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

        run(["git", "push", "-u", "origin", child], cwd=worktree, exit_on_error=True)
