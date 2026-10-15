"""Sync a stack: merge each parent into its child, restacking when one lands.

Day-to-day this merges (not rebases) root -> leaf: no force-push, and a conflict
is resolved with a single ``git commit`` + re-run (idempotent). When a parent's
PR has merged, its children are rebased onto the grandparent instead
("restack-on-land") and the landed branch is cleaned up. A child whose parent
was rewritten (restacked, or squashed) still holds the old copies of the
parent's commits, so merging the parent would conflict with itself: the child's
own commits are replayed onto the parent instead. These are the only places we
force-push / rewrite history. Whether a parent landed is decided by the injected
`Forge`, so it is authoritative (a squash-merge can't be detected from local
ancestry) and testable.

Worktree-per-branch is assumed: each branch is synced inside its own worktree.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

import typer

from toolbelt.git.exec import run
from toolbelt.git.stack.forge import Forge
from toolbelt.git.stack.gh_stack import StackedPrs, mirror_stack
from toolbelt.git.stack.lineage import (
    all_parents,
    get_pr_link,
    link_pr,
    remove_parent,
    resolve_stack,
    set_parent,
)
from toolbelt.git.stack.ops import (
    has_unpushed_real_work,
    past_tips,
    tree_differs_from_remote,
)
from toolbelt.git.stack.store import PrCandidate
from toolbelt.git.stack.worktree import worktree_paths
from toolbelt.git.worktrees import current_branch
from toolbelt.git.worktrees_ops import (
    delete_branch_and_worktree,
    ensure_not_inside_worktree,
    main_worktree,
)
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


_MAX_CONFLICT_FILES_LOGGED = 10


@dataclass(frozen=True)
class ConflictDetails:
    """What a stopped rebase or merge is stuck on.

    ``commit`` is the short hash and subject of the commit being applied (the
    rebase's ``REBASE_HEAD``) or the tip being merged (``MERGE_HEAD``); None when
    git has no such ref. ``step``/``steps`` give the commit's position in a
    rebase (None for a merge or when the rebase state is unreadable).
    """

    rebasing: bool
    commit: str | None
    step: int | None
    steps: int | None
    files: tuple[str, ...]


def _commit_summary(worktree: Path, ref: str) -> str | None:
    """``<short hash> "<subject>"`` of ``ref``, or None if it does not exist."""
    result = run(
        ["git", "log", "-1", '--format=%h "%s"', ref, "--"],
        cwd=worktree,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _todo_lines(path: Path | None) -> int | None:
    """Number of commands in a rebase todo-style file, None if it is missing."""
    if path is None or not path.exists():
        return None
    return sum(
        1
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )


def _rebase_position(worktree: Path) -> tuple[int, int] | None:
    """(step, steps) of the commit a ``rebase-merge`` is stopped on."""
    done = _todo_lines(_git_path(worktree, "rebase-merge/done"))
    todo = _todo_lines(_git_path(worktree, "rebase-merge/git-rebase-todo"))
    if done is None or todo is None:
        return None
    return done, done + todo


def read_conflict(worktree: Path) -> ConflictDetails:
    """Describe the conflict ``worktree`` is stopped on (mid-rebase or mid-merge)."""
    rebasing = _rebase_in_progress(worktree)
    commit = _commit_summary(worktree, "REBASE_HEAD" if rebasing else "MERGE_HEAD")
    position = _rebase_position(worktree) if rebasing else None
    files = run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=worktree,
        check=False,
        capture_output=True,
    ).stdout.splitlines()
    return ConflictDetails(
        rebasing=rebasing,
        commit=commit,
        step=position[0] if position else None,
        steps=position[1] if position else None,
        files=tuple(files),
    )


def _log_conflict(worktree: Path, *, merge_ref: str | None = None) -> None:
    """Log which commit a stopped rebase/merge came from and the conflicted files.

    ``merge_ref`` names the ref being merged, when known.
    """
    details = read_conflict(worktree)
    if details.commit is not None:
        if details.rebasing:
            position = (
                f" (commit {details.step} of {details.steps})"
                if details.step is not None
                else ""
            )
            logger.error(f"Conflict in commit {details.commit}{position}.")
        else:
            name = f"'{merge_ref}' " if merge_ref else ""
            logger.error(f"Conflict merging {name}(tip {details.commit}).")
    shown = details.files[:_MAX_CONFLICT_FILES_LOGGED]
    if shown:
        hidden = len(details.files) - len(shown)
        more = f" and {hidden} more" if hidden else ""
        logger.error(f"Conflicted files: {', '.join(shown)}{more}.")


def _remote_branch_exists(branch: str, *, root: Path) -> bool:
    result = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


async def _resolve_stack_status(
    stack: list[str], unlinked: list[str], forge: Forge
) -> tuple[set[str], set[str], set[str], dict[str, list[PrCandidate]]]:
    """Ask `forge` which of `stack`'s branches have landed, which have an
    open PR (draft or not), and which of those PRs conflict with their base
    — and, for the ``unlinked`` ones (no PR recorded for their task yet),
    which PRs they have — all concurrently, across the whole stack in one
    round trip."""
    merged, is_open, conflicting, found = await asyncio.gather(
        asyncio.gather(*(forge.pr_is_merged(b) for b in stack)),
        asyncio.gather(*(forge.pr_is_open(b) for b in stack)),
        asyncio.gather(*(forge.pr_is_conflicting(b) for b in stack)),
        asyncio.gather(*(forge.pr_candidates(b) for b in unlinked)),
    )
    landed = {branch for branch, m in zip(stack, merged) if m}
    has_open_pr = {branch for branch, o in zip(stack, is_open) if o}
    conflicts = {branch for branch, c in zip(stack, conflicting) if c}
    return landed, has_open_pr, conflicts, dict(zip(unlinked, found))


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


def _count(rev_range: list[str], *, worktree: Path) -> int:
    return int(
        run(
            ["git", "rev-list", "--count", *rev_range],
            cwd=worktree,
            capture_output=True,
        ).stdout.strip()
    )


def _replay_base(child: str, parent: str, *, onto: str, worktree: Path) -> str:
    """Where the child's own commits begin, for rebasing it off its landed
    parent onto ``onto``.

    Normally that is the parent's tip: ``parent..child`` is the child's own
    commits. But a child already rebased onto the base (say by hand after a
    conflict) holds everything on the base since the parent too, and replaying
    those onto the newer base conflicts with itself and with other people's
    work. The commits reachable from ``onto`` are not the child's own either,
    so the fork point with ``onto`` is the other base; whichever replays fewer
    commits is the child's own (a tie keeps the parent). With no shared
    history with ``onto`` the parent is kept and the rebase reports what is
    wrong.
    """
    fork = run(
        ["git", "merge-base", child, onto],
        cwd=worktree,
        check=False,
        capture_output=True,
    )
    if fork.returncode != 0:
        return parent
    fork_point = fork.stdout.strip()
    if _count([f"{fork_point}..{child}"], worktree=worktree) < _count(
        [f"{parent}..{child}"], worktree=worktree
    ):
        return fork_point
    return parent


def _is_ancestor(ancestor: str, descendant: str, *, worktree: Path) -> bool:
    return (
        run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=worktree,
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _rewritten_parent_tip(child: str, parent: str, *, worktree: Path) -> str | None:
    """The old tip of ``parent`` that ``child`` still builds on, when ``parent``
    has been rewritten since: the commit after which ``child``'s own commits begin.

    The newest commit ``parent`` has pointed at (per its reflog) that ``child``
    contains is the last point ``child`` took from it. If that commit is still
    reachable from ``parent`` the parent only moved forward and merging brings
    ``child`` up to date. If not, ``child`` holds old copies of commits ``parent``
    has replaced, and merging the new ones in conflicts with them. None when
    ``child`` is current, or contains nothing ``parent`` ever pointed at (the
    reflog is gone), so merging is the only thing to do.
    """
    for tip in past_tips(parent, root=worktree):
        if _is_ancestor(tip, child, worktree=worktree):
            if _is_ancestor(tip, parent, worktree=worktree):
                return None
            return tip
    return None


def sync_stack(
    *,
    root: Path,
    forge: Forge,
    stacked_prs: StackedPrs | None = None,
    branch: str | None = None,
) -> None:
    """Merge-sync the whole stack the current branch belongs to.

    ``branch`` names the stack to sync by one of its branches, for a caller
    running in a worktree that is not part of it (the main worktree, after
    landing a branch whose own worktree is about to be removed).

    Processes branches root -> leaf so each parent is up to date before being
    merged into its children. The stack root merges ``origin/<base>`` so landed
    work propagates down. When ``forge`` reports a parent's PR merged, that
    parent's children are rebased onto the nearest surviving ancestor and every
    landed branch is removed.

    When ``stacked_prs`` is given, the synced stack is mirrored to GitHub's
    native stacked PRs once everything above has succeeded.
    """
    run(["git", "fetch", "-p"], cwd=root, exit_on_error=True)

    # When resuming after a restack conflict, this worktree is mid-rebase and
    # its HEAD is detached; recover the branch from the rebase state.
    rebasing = _branch_being_rebased(root)
    branch = branch or rebasing or current_branch(root)
    parents = all_parents(root=root)
    tracked = set(parents.keys())
    if branch not in tracked:
        logger.error(
            f"'{branch}' is not part of a tracked stack. Start one with "
            "`git append <name>`."
        )
        raise typer.Exit(1)

    stack = resolve_stack(branch, root=root)
    paths = worktree_paths(root=root)
    # A mid-rebase worktree shows as detached in `git worktree list`, so map the
    # branch being rebased back to this worktree.
    if rebasing is not None:
        paths.setdefault(rebasing, root)

    # Authoritative, queried once per branch (concurrently — each is an
    # independent `gh` network round-trip).
    unlinked = [b for b in stack if get_pr_link(b, root=root) is None]
    landed, has_open_pr, has_conflicts, candidates = asyncio.run(
        _resolve_stack_status(stack, unlinked, forge)
    )
    # Record each task's PR now, while every branch (landed ones included) is
    # still tracked: cleanup below archives the row, and the PR's identity is
    # what the UI joins history on.
    for branch, found in candidates.items():
        link_pr(branch, found, root=root)

    # Landed branches are deleted at the end; refuse up front (before
    # restacking or deleting anything) if that would remove the worktree this
    # process is running in.
    main_wt = main_worktree(root)
    for landed_branch in sorted(landed):
        ensure_not_inside_worktree(
            landed_branch, root=main_wt, caller_dirs=[root, Path.cwd()]
        )

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
                "its own worktree (create it with `git append`)."
            )
            raise typer.Exit(1)

        direct_parent = parents[child]
        resuming = _rebase_in_progress(worktree)
        # An unfinished merge is concluded (or reported) below, as before.
        rewritten_from = (
            _rewritten_parent_tip(child, direct_parent, worktree=worktree)
            if direct_parent in tracked
            and direct_parent not in landed
            and not resuming
            and not _merge_in_progress(worktree)
            else None
        )

        if direct_parent in landed:
            # Replay child's own commits (those after its direct parent) onto the
            # nearest surviving ancestor, dropping every landed ancestor's work
            # (already on the base via their squashes). Handles N stacked lands.
            base = surviving_base(child)
            onto = base if base in tracked else f"origin/{base}"

            if not _restack(
                child,
                worktree=worktree,
                onto=onto,
                upstream=_replay_base(
                    child, direct_parent, onto=onto, worktree=worktree
                ),
            ):
                _log_conflict(worktree)
                logger.error(
                    f"Rebase conflict restacking '{child}' onto '{base}' in "
                    f"{worktree}. Resolve, `git add`, then re-run `git sync`."
                )
                raise typer.Exit(1)

            set_parent(child, base, root=root)
            run(
                ["git", "push", "--force-with-lease", "origin", child],
                cwd=worktree,
                exit_on_error=True,
            )
        elif resuming or rewritten_from is not None:
            # The parent was rewritten: replay the child's own commits onto it
            # rather than merging its new commits into the old copies. Also
            # resumes a replay that stopped on a conflict.
            if not _restack(
                child,
                worktree=worktree,
                onto=direct_parent,
                upstream=rewritten_from or direct_parent,
            ):
                _log_conflict(worktree)
                logger.error(
                    f"Rebase conflict replaying '{child}' onto its rewritten "
                    f"parent '{direct_parent}' in {worktree}. Resolve, "
                    "`git add`, then re-run `git sync`."
                )
                raise typer.Exit(1)
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
                    _log_conflict(worktree)
                    logger.error(
                        f"Unresolved merge in '{child}' ({worktree}). Resolve, "
                        "`git add`, then re-run `git sync`."
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
                    _log_conflict(worktree, merge_ref=merge_ref)
                    logger.error(
                        f"Merge conflict while syncing '{child}' in {worktree}. "
                        "Resolve the conflict, `git add`, then re-run `git sync`."
                    )
                    raise typer.Exit(1)

            # A branch with children is always pushed: they track it. A leaf
            # is pushed only when there's an open PR to show it to *and* a
            # reason to: new work of its own, the PR conflicts with its base
            # (which this sync's merge may just have resolved; the push is
            # what clears it on GitHub), or its tree now differs from
            # origin's because the merge absorbed parent changes the PR has
            # not seen (otherwise the PR and its CI stay stale relative to
            # the parent). A no-op sync leaves the tree equal to origin's, so
            # it pushes nothing and re-triggers no CI or review.
            has_children = any(
                parent == child and other not in landed
                for other, parent in parents.items()
            )
            if (
                has_children
                or child in has_open_pr
                and (
                    child in has_conflicts
                    or has_unpushed_real_work(child, root=worktree)
                    or tree_differs_from_remote(child, root=worktree)
                )
            ):
                run(
                    ["git", "push", "-u", "origin", child],
                    cwd=worktree,
                    exit_on_error=True,
                )
            else:
                logger.info(
                    f"'{child}' is up to date locally; not pushing "
                    "(nothing new relative to origin)."
                )

    # Every landed branch has had its children restacked away, so each is now a
    # leaf in the lineage and safe to remove (branch, worktree, and config key).
    # Run from the main worktree: git can't remove a worktree it runs in.
    for landed_branch in landed:
        landed_wt = paths.get(landed_branch)
        if landed_wt is not None and landed_wt.resolve() == main_wt.resolve():
            logger.warning(
                f"Cannot remove '{landed_branch}': it is checked out in the main "
                "worktree. Switch it to the base branch and re-run `git sync`."
            )
            continue
        delete_branch_and_worktree(landed_branch, repo_root=main_wt, force=True)
        remove_parent(landed_branch, root=main_wt, landed=True)
        logger.info(f"Cleaned up landed branch '{landed_branch}'.")

    if stacked_prs is not None:
        # `root` may be the worktree of a landed branch that was just removed.
        mirror_stack(stack, root=main_wt, stacked_prs=stacked_prs)
