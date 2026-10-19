"""Sync a stack: merge each parent into its child, restacking when one lands.

Day-to-day this merges (not rebases) root -> leaf: no force-push, and a conflict
is resolved with a single ``git commit`` + re-run (idempotent). When a parent's
PR has merged, its children get the base it landed on merged in and are
reparented onto the grandparent ("restack-on-land"), and the landed branch is
cleaned up. That merge is computed over snapshots (the child's tree, the base's
tree and the parent's last tip the child contains), so it rewrites no history
and is only pushed as a fast-forward. Only when it conflicts, or the child's
remote has diverged from the child, are the child's own commits replayed onto
the base instead (and force-pushed). A child whose parent
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
    is_fast_forward_ahead_of_remote,
    past_tips,
    publish_untracked_branch,
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


def branch_being_rebased(worktree: Path) -> str | None:
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


def _last_parent_tip_in(child: str, parent: str, *, worktree: Path) -> str | None:
    """The newest commit ``parent`` has pointed at (per its reflog) that ``child``
    contains: the tip ``child`` was last based on. Falls back to the fork point
    of ``child`` and ``parent``; None when they share no history."""
    for tip in past_tips(parent, root=worktree):
        if _is_ancestor(tip, child, worktree=worktree):
            return tip
    fork = run(
        ["git", "merge-base", child, parent],
        cwd=worktree,
        check=False,
        capture_output=True,
    )
    return fork.stdout.strip() if fork.returncode == 0 else None


def _worktree_is_clean(worktree: Path) -> bool:
    """True if ``worktree`` has no staged or unstaged change to tracked files."""
    return not run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=worktree,
        capture_output=True,
    ).stdout.strip()


def _adopt_remote_restack(child: str, *, worktree: Path, base: str, onto: str) -> bool:
    """Take over ``origin/<child>`` when it is already the restacked ``child``.

    GitHub restacks the open PRs above a PR it merges (rebasing their branches
    on the remote), so ``origin/<child>`` can hold a rewritten copy of the
    child that local never had. When that copy contains ``onto`` and its tree is
    what merging ``onto`` into the child over snapshots (three-way, with
    ``base`` as the merge base) produces, nothing is lost by moving the local
    branch to it: its work is on the remote already, and replaying the child's
    own commits would only force-push a second copy over it. Needs a clean
    worktree. The previous local tip stays in the reflog.
    """
    remote = f"origin/{child}"
    if (
        not _remote_branch_exists(child, root=worktree)
        or _is_ancestor(remote, child, worktree=worktree)
        or not _is_ancestor(onto, remote, worktree=worktree)
        or not _worktree_is_clean(worktree)
    ):
        return False
    merged = run(
        [
            "git",
            "merge-tree",
            "--write-tree",
            f"--merge-base={base}",
            child,
            onto,
        ],
        cwd=worktree,
        check=False,
        capture_output=True,
    )
    if merged.returncode != 0:
        return False
    remote_tree = run(
        ["git", "rev-parse", f"{remote}^{{tree}}"], cwd=worktree, capture_output=True
    ).stdout.strip()
    if remote_tree != merged.stdout.split()[0]:
        return False
    run(["git", "reset", "--hard", remote], cwd=worktree, exit_on_error=True)
    logger.info(
        f"'{child}' was already restacked on origin (by GitHub or another "
        f"checkout) with the content a restack here would give; moved to {remote}."
    )
    return True


def _remote_has_foreign_commits(child: str, *, worktree: Path) -> bool:
    """True if ``origin/<child>`` is not part of ``child``'s history: it is not
    an ancestor of ``child`` and ``child`` never pointed at it (per its reflog).

    That is a remote rewritten or extended by someone else (GitHub restacking
    after a parent merged, another checkout), as opposed to a remote left behind
    by the user rewriting the branch locally; force-pushing over it would
    destroy commits this checkout has never seen.
    """
    if not _remote_branch_exists(child, root=worktree):
        return False
    remote = f"origin/{child}"
    if _is_ancestor(remote, child, worktree=worktree):
        return False
    remote_tip = run(
        ["git", "rev-parse", remote], cwd=worktree, capture_output=True
    ).stdout.strip()
    return remote_tip not in past_tips(child, root=worktree)


def _refuse_replay_over_foreign_remote(
    child: str, *, worktree: Path, onto: str
) -> None:
    """Stop, before changing anything, if replaying ``child`` would force-push
    over commits on ``origin/<child>`` that this checkout never had."""
    if not _remote_has_foreign_commits(child, worktree=worktree):
        return
    local, remote = (
        sha[:8]
        for sha in run(
            ["git", "rev-parse", child, f"origin/{child}"],
            cwd=worktree,
            capture_output=True,
        ).stdout.split()
    )
    logger.error(
        f"Not restacking '{child}' onto '{onto}' in {worktree}: origin/{child} "
        f"({remote}) holds commits '{child}' ({local}) has never had, and its "
        "content is not what a restack here would produce, so replaying and "
        "force-pushing would overwrite them. Nothing was changed: the branch is "
        f"at {local} and origin/{child} at {remote}. Compare them "
        f"(`git diff {child} origin/{child}`), then merge origin/{child} into "
        f"'{child}' or reset '{child}' to it, and re-run `git sync`."
    )
    raise typer.Exit(1)


def _force_push_replay(child: str, *, worktree: Path) -> None:
    """Force-push the replayed ``child``, leased to the remote tip the replay
    started from; if origin moved since, say what state that leaves."""
    lease = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{child}"],
        cwd=worktree,
        check=False,
        capture_output=True,
    ).stdout.strip()
    run(["git", "fetch", "origin", child], cwd=worktree, check=False)
    now = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{child}"],
        cwd=worktree,
        check=False,
        capture_output=True,
    ).stdout.strip()
    if now != lease:
        logger.error(
            f"origin/{child} moved while '{child}' was being restacked "
            f"({lease[:8] or 'none'} -> {now[:8]}). '{child}' is replayed "
            f"locally in {worktree} but NOT pushed; origin/{child} is untouched "
            f"at {now[:8]}. Inspect `git diff {child} origin/{child}`, merge or "
            "reset as appropriate, then re-run `git sync`."
        )
        raise typer.Exit(1)
    pushed = run(
        ["git", "push", f"--force-with-lease={child}:{lease}", "origin", child],
        cwd=worktree,
        check=False,
    )
    if pushed.returncode != 0:
        logger.error(
            f"Force-push of '{child}' was rejected. '{child}' is replayed locally "
            f"in {worktree} but NOT pushed; origin/{child} is unchanged. Inspect "
            f"`git diff {child} origin/{child}`, then re-run `git sync`."
        )
        raise typer.Exit(1)


def _merge_landed_parent(child: str, *, worktree: Path, parent: str, onto: str) -> bool:
    """Bring ``child`` up to date with the base its landed ``parent`` was
    squashed into, by merging ``onto`` in, without rewriting any history.

    The merge is three-way over snapshots with ``parent``'s last tip that
    ``child`` contains as the base, so what the child already has of the
    parent's work is not applied twice. The merge commit has ``child``'s tip as
    first parent and ``onto`` as second, so it only ever fast-forwards the
    child's remote and keeps conflict resolutions that live in the child's
    merge commits (a replay of the child's non-merge commits drops those).
    Does nothing if ``child`` already contains ``onto``.

    Returns True once ``child`` contains ``onto``. False means the caller must
    restack the old way (replay and force-push): the merge conflicts (``onto``
    changed what the child changed), the child's remote is not an ancestor of
    the child (the push would need a force), or the worktree cannot be fast
    forwarded.
    """
    if _is_ancestor(onto, child, worktree=worktree):
        return True
    base = _last_parent_tip_in(child, parent, worktree=worktree)
    if base is None:
        return False
    if _adopt_remote_restack(child, worktree=worktree, base=base, onto=onto):
        return True
    if _remote_branch_exists(child, root=worktree) and not _is_ancestor(
        f"origin/{child}", child, worktree=worktree
    ):
        return False
    tree = run(
        [
            "git",
            "merge-tree",
            "--write-tree",
            f"--merge-base={base}",
            child,
            onto,
        ],
        cwd=worktree,
        check=False,
        capture_output=True,
    )
    if tree.returncode != 0:
        return False
    merged = run(
        [
            "git",
            "commit-tree",
            tree.stdout.split()[0],
            "-p",
            child,
            "-p",
            onto,
            "-m",
            f"Merge {onto} into {child}",
        ],
        cwd=worktree,
        capture_output=True,
    ).stdout.strip()
    return (
        run(
            ["git", "merge", "--ff-only", merged],
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
    rebasing = branch_being_rebased(root)
    branch = branch or rebasing or current_branch(root)
    parents = all_parents(root=root)
    tracked = set(parents.keys())
    if branch not in tracked:
        if publish_untracked_branch(branch, root=root):
            return
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

            if (
                not resuming
                and not _merge_in_progress(worktree)
                and _merge_landed_parent(
                    child, worktree=worktree, parent=direct_parent, onto=onto
                )
            ):
                # Merged, not replayed: the push is a fast-forward.
                set_parent(child, base, root=root)
                if not _remote_branch_exists(
                    child, root=root
                ) or is_fast_forward_ahead_of_remote(child, root=worktree):
                    run(
                        ["git", "push", "-u", "origin", child],
                        cwd=worktree,
                        exit_on_error=True,
                    )
                continue

            if not resuming:
                _refuse_replay_over_foreign_remote(child, worktree=worktree, onto=base)
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
            _force_push_replay(child, worktree=worktree)
        elif resuming or rewritten_from is not None:
            # The parent was rewritten: replay the child's own commits onto it
            # rather than merging its new commits into the old copies. Also
            # resumes a replay that stopped on a conflict.
            if rewritten_from is not None and _adopt_remote_restack(
                child, worktree=worktree, base=rewritten_from, onto=direct_parent
            ):
                continue
            if not resuming:
                _refuse_replay_over_foreign_remote(
                    child, worktree=worktree, onto=direct_parent
                )
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
            _force_push_replay(child, worktree=worktree)
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

            # Judged before the merges below: a branch that diverged from
            # origin is reconciled by merging origin in, which would make it
            # look ahead afterwards; only a branch already ahead of origin
            # on its own qualifies.
            ahead_of_remote = is_fast_forward_ahead_of_remote(child, root=worktree)
            # origin has never seen it: publish it (with upstream tracking)
            # whatever its PR state, since there is nothing to compare to.
            unpublished = not _remote_branch_exists(child, root=root)

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
            # it pushes nothing and re-triggers no CI or review. Open PR or
            # not, a branch origin does not have yet is published, and one it
            # has that is strictly ahead of it is fast-forwarded (never
            # forced), so origin does not lag a branch others build on.
            has_children = any(
                parent == child and other not in landed
                for other, parent in parents.items()
            )
            if (
                has_children
                or unpublished
                or ahead_of_remote
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
