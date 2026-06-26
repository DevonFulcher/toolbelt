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
from toolbelt.git.stack.lineage import (
    all_parents,
    remove_parent,
    resolve_stack,
    set_parent,
)
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


def _merge_in_progress(worktree: Path) -> bool:
    """True if ``worktree`` has an unfinished merge (a prior conflict)."""
    path = _git_path(worktree, "MERGE_HEAD")
    return path is not None and path.exists()


def _has_unmerged_paths(worktree: Path) -> bool:
    """True if ``worktree`` still has unresolved conflict entries staged."""
    result = run(
        ["git", "ls-files", "--unmerged"],
        cwd=worktree,
        capture_output=True,
    )
    return bool(result.stdout.strip())


def _remote_branch_exists(branch: str, *, root: Path) -> bool:
    result = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _main_worktree(root: Path) -> Path:
    """The repo's main working tree (first entry of ``git worktree list``).

    Cleanup runs from here so a landed worktree can be removed even when it is
    the caller's current directory (git only refuses to remove the worktree the
    git process itself is running in).
    """
    result = run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=root,
        capture_output=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree ") :])
    return root


def _restack(child: str, *, worktree: Path, onto: str, upstream: str) -> bool:
    """Replay ``child``'s own commits (those after ``upstream``) onto ``onto``.

    Resumes if the worktree is already mid-rebase. Returns True on success,
    False if a conflict left the rebase in progress.
    """
    if _rebase_in_progress(worktree):
        result = run(["git", "rebase", "--continue"], cwd=worktree, check=False)
    else:
        result = run(
            ["git", "rebase", "--onto", onto, upstream, child],
            cwd=worktree,
            check=False,
        )
    return result.returncode == 0


def sync_stack(*, root: Path, forge: Forge) -> None:
    """Merge-sync the whole stack the current branch belongs to.

    Processes branches root -> leaf so each parent is up to date before being
    merged into its children. The stack root merges ``origin/<base>`` so landed
    work propagates down. When ``forge`` reports a parent's PR merged, that
    parent's children are rebased onto the nearest surviving ancestor and every
    landed branch is removed.
    """
    run(["git", "fetch", "-p"], cwd=root, exit_on_error=True)

    # When resuming after a restack conflict, this worktree is mid-rebase and
    # its HEAD is detached; recover the branch from the rebase state.
    rebasing = _branch_being_rebased(root)
    branch = rebasing or current_branch(root)
    parents = all_parents(root=root)
    tracked = set(parents.keys())
    if branch not in tracked:
        logger.error(
            f"'{branch}' is not part of a tracked stack. Start one with "
            "`stack append <name>`."
        )
        raise typer.Exit(1)

    stack = resolve_stack(branch, root=root)
    paths = worktree_paths(root=root)
    # A mid-rebase worktree shows as detached in `git worktree list`, so map the
    # branch being rebased back to this worktree.
    if rebasing is not None:
        paths.setdefault(rebasing, root)

    # Authoritative, queried once per branch.
    landed = {b for b in stack if forge.pr_is_merged(b)}

    def surviving_base(child: str) -> str:
        """Nearest ancestor of ``child`` that has not landed (collapses chains
        of consecutive landed ancestors so cascading lands resolve correctly)."""
        ancestor = parents[child]
        while ancestor in landed:
            ancestor = parents[ancestor]
        return ancestor

    for child in stack:
        if child in landed:
            # Its PR merged; skip syncing — removed during cleanup below.
            continue

        worktree = paths.get(child)
        if worktree is None:
            logger.error(
                f"No worktree found for '{child}'. Every stacked branch needs "
                "its own worktree (create it with `stack append`)."
            )
            raise typer.Exit(1)

        direct_parent = parents[child]

        if direct_parent in landed:
            # Replay child's own commits (those after its direct parent) onto the
            # nearest surviving ancestor, dropping every landed ancestor's work
            # (already on the base via their squashes). Handles N stacked lands.
            base = surviving_base(child)
            onto = base if base in tracked else f"origin/{base}"

            if not _restack(
                child, worktree=worktree, onto=onto, upstream=direct_parent
            ):
                logger.error(
                    f"Rebase conflict restacking '{child}' onto '{base}' in "
                    f"{worktree}. Resolve, `git add`, then re-run `stack sync`."
                )
                raise typer.Exit(1)

            set_parent(child, base, root=root)
            run(
                ["git", "push", "--force-with-lease", "origin", child],
                cwd=worktree,
                exit_on_error=True,
            )
        else:
            # Conclude a prior conflicted merge the user has since resolved
            # (symmetric with the rebase --continue resume path).
            if _merge_in_progress(worktree):
                if _has_unmerged_paths(worktree):
                    logger.error(
                        f"Unresolved merge in '{child}' ({worktree}). Resolve, "
                        "`git add`, then re-run `stack sync`."
                    )
                    raise typer.Exit(1)
                run(["git", "commit", "--no-edit"], cwd=worktree, exit_on_error=True)

            # Integrate the parent (base from the remote) and reconcile the
            # branch with its own remote so a diverged push won't be rejected.
            # The base (e.g. main) has no lineage entry; pull it from origin.
            merge_refs = [
                direct_parent if direct_parent in tracked else f"origin/{direct_parent}"
            ]
            if _remote_branch_exists(child, root=root):
                merge_refs.append(f"origin/{child}")

            for merge_ref in merge_refs:
                result = run(
                    ["git", "merge", "--no-edit", merge_ref],
                    cwd=worktree,
                    check=False,
                )
                if result.returncode != 0:
                    logger.error(
                        f"Merge conflict while syncing '{child}' in {worktree}. "
                        "Resolve the conflict, `git add`, then re-run `stack sync`."
                    )
                    raise typer.Exit(1)
            run(
                ["git", "push", "-u", "origin", child],
                cwd=worktree,
                exit_on_error=True,
            )

    # Every landed branch has had its children restacked away, so each is now a
    # leaf in the lineage and safe to remove (branch, worktree, and config key).
    # Run from the main worktree so a landed branch can be removed even when it
    # is the caller's current worktree.
    main_wt = _main_worktree(root)
    for landed_branch in landed:
        landed_wt = paths.get(landed_branch)
        if landed_wt is not None and landed_wt.resolve() == main_wt.resolve():
            logger.warning(
                f"Cannot remove '{landed_branch}': it is checked out in the main "
                "worktree. Switch it to the base branch and re-run `stack sync`."
            )
            continue
        delete_branch_and_worktree(landed_branch, repo_root=main_wt, force=True)
        remove_parent(landed_branch, root=main_wt)
        logger.info(f"Cleaned up landed branch '{landed_branch}'.")
