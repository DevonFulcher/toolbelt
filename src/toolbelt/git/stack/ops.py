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


_ATTRIBUTION_TRAILER = "Co-Authored-By"
# Marks a commit made by collapse_trailing_merges, so a later collapse can
# see past it (it's flattened to one parent, indistinguishable from a real
# commit otherwise) instead of stopping there and leaving sync noise to
# accumulate one commit per sync.
_SYNC_COLLAPSE_TRAILER = "Toolbelt-Sync-Collapse"


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


def _is_ancestor(ancestor: str, descendant: str, *, root: Path) -> bool:
    result = run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _rev_parse(ref: str, *, root: Path) -> str:
    return run(["git", "rev-parse", ref], cwd=root, capture_output=True).stdout.strip()


def _parent_ref(parent: str, *, root: Path) -> str:
    """The ref to measure a branch against its stack parent.

    A parent that is itself in the stack is current locally: ``sync`` merges it
    into its children straight from the local branch. The stack's base (e.g.
    ``main``) is not tracked, ``sync`` merges ``origin/<base>`` into the stack
    root, and nothing updates the local base branch, so it is often behind the
    remote. Measuring against that stale branch treats every newer base commit
    already merged into the branch as the branch's own work, and a squash then
    folds them in. Use ``origin/<base>`` when the local base is behind it; keep
    the local base when it is ahead of, or has diverged from, the remote.
    """
    if parent in all_parents(root=root):
        return parent
    run(["git", "fetch", "origin", parent], cwd=root, check=False, capture_output=True)
    remote = f"origin/{parent}"
    if (
        _remote_branch_exists(parent, root=root)
        and _rev_parse(parent, root=root) != _rev_parse(remote, root=root)
        and _is_ancestor(parent, remote, root=root)
    ):
        return remote
    return parent


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
    """The full message (subject, body, trailers) of the branch's oldest own commit.

    ``base..HEAD`` can still hold commits the parent has since rewritten away
    (e.g. squashed by ``sync``), because the branch forked from or merged them.
    Excluding everything reachable from any tip the parent has ever had, plus
    merge commits, leaves the branch's own commits. If that leaves nothing,
    falls back to the oldest commit in ``base..HEAD``.

    The parent's past tips go on the command line; branch reflogs expire and
    stay small, so this is well under the argument-length limit.
    """
    own = run(
        [
            "git",
            "log",
            "--reverse",
            "--no-merges",
            "--format=%H",
            f"{base}..HEAD",
            "--not",
            *_past_tips(parent, root=root),
        ],
        cwd=root,
        capture_output=True,
    ).stdout.split()
    if own:
        chosen = own[0]
    else:
        chosen = run(
            ["git", "log", "--reverse", "--format=%H", f"{base}..HEAD"],
            cwd=root,
            capture_output=True,
        ).stdout.split()[0]
    message = run(
        ["git", "log", "-1", "--format=%B", chosen], cwd=root, capture_output=True
    ).stdout.strip()
    return _with_attribution_trailers(message, base=base, root=root)


def _with_attribution_trailers(message: str, *, base: str, root: Path) -> str:
    """``message`` plus every ``Co-Authored-By`` trailer from ``base..HEAD``.

    Squashing drops all but one commit's message, so attribution trailers from
    the other commits are carried over. ``git interpret-trailers`` adds each one
    to the message's trailer block (starting one if needed) unless an identical
    trailer is already there.
    """
    found = run(
        [
            "git",
            "log",
            "--reverse",
            "--no-merges",
            f"--format=%(trailers:key={_ATTRIBUTION_TRAILER},unfold)",
            f"{base}..HEAD",
        ],
        cwd=root,
        capture_output=True,
    ).stdout.splitlines()
    trailers = list(dict.fromkeys(line.strip() for line in found if line.strip()))
    if not trailers:
        return message
    args = [
        "git",
        "interpret-trailers",
        "--no-divider",
        "--where",
        "end",
        "--if-exists",
        "addIfDifferent",
    ]
    for trailer in trailers:
        args += ["--trailer", trailer]
    return run(args, cwd=root, capture_output=True, input=message + "\n").stdout.strip()


def compress_branch(
    *,
    root: Path,
    message: str | None = None,
    push: bool = True,
    since: str | None = None,
) -> None:
    """Squash the current branch's own commits (those after its parent) into one.

    Rewrites history, so the branch's remote is force-pushed to match (unless
    ``push`` is False — ``sync`` sets this since it does its own push right
    after, for every branch, compressed or not). Children are left untouched,
    but they still contain the commits that were squashed, so merging this
    branch into them afterwards can report false conflicts. ``sync`` only
    compresses leaves for that reason.

    ``since`` overrides the squash boundary (normally the branch's merge-base
    with its parent) with an arbitrary commit — see ``collapse_trailing_merges``,
    which uses this to squash only a trailing run of merge commits rather than
    the whole branch.
    """
    branch = current_branch(root)
    parent = _parent_or_exit(branch, root=root)

    base = (
        since
        or run(
            ["git", "merge-base", _parent_ref(parent, root=root), "HEAD"],
            cwd=root,
            capture_output=True,
        ).stdout.strip()
    )

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
        # Default to the branch's first (oldest) commit message, like git-town.
        message = _default_message(parent=parent, base=base, root=root)

    # Soft reset keeps the working tree and index, so the commit captures every
    # change since the fork point as one commit; unstaged work is left alone.
    run(["git", "reset", "--soft", base], cwd=root, exit_on_error=True)
    # The message goes over stdin so a multi-line body survives intact; the
    # explicit cleanup mode keeps a body line starting with ``#`` from being
    # treated as a comment.
    run(
        ["git", "commit", "--cleanup=whitespace", "-F", "-"],
        cwd=root,
        exit_on_error=True,
        input=message.strip() + "\n",
    )
    logger.info(f"Compressed {count} commits on '{branch}' into one.")

    if push and _remote_branch_exists(branch, root=root):
        run(
            ["git", "push", "--force-with-lease", "origin", branch],
            cwd=root,
            exit_on_error=True,
        )
        logger.info(f"Force-pushed '{branch}'.")


def _is_sync_collapse_commit(commit: str, *, root: Path) -> bool:
    value = run(
        [
            "git",
            "log",
            "-1",
            f"--format=%(trailers:key={_SYNC_COLLAPSE_TRAILER},valueonly,unfold)",
            commit,
        ],
        cwd=root,
        capture_output=True,
    ).stdout.strip()
    return value == "true"


def _trailing_merge_anchor(*, base: str, root: Path) -> str:
    """The newest commit in ``base..HEAD`` that is neither a merge nor a prior
    collapse (see ``_SYNC_COLLAPSE_TRAILER``), walking HEAD's first-parent
    chain — i.e. the boundary right before any trailing run of sync noise.
    ``base`` itself if every commit since is sync noise.

    If HEAD itself isn't a merge, there's nothing new to fold in since
    whatever's at the tip (a real commit, or a prior collapse nothing has
    been synced on top of yet) — return it immediately rather than walking
    further back, which would otherwise needlessly re-collapse a prior,
    already-settled collapse on every sync even when nothing changed.

    Once HEAD is a fresh merge, the walk back *does* see past a prior
    collapse, not just literal merges — that's what keeps sync noise to at
    most one commit no matter how many times a branch gets synced: a
    collapse flattens a merge to one parent, so without this a later
    collapse couldn't tell it apart from a real commit and would stop there,
    leaving one more noise commit behind every single sync.
    """
    log = run(
        ["git", "log", "--first-parent", "--format=%H %P", f"{base}..HEAD"],
        cwd=root,
        capture_output=True,
    ).stdout.splitlines()
    if not log:
        return base
    head_commit, *head_parents = log[0].split()
    if len(head_parents) < 2:
        return head_commit
    for line in log:
        commit, *parents = line.split()
        if len(parents) >= 2 or _is_sync_collapse_commit(commit, root=root):
            continue
        return commit
    return base


def collapse_trailing_merges(*, root: Path, push: bool = True) -> None:
    """Collapse the current branch's trailing run of sync noise (merge
    commits, and prior collapses of them) since its last real commit into
    one, via ``compress_branch``.

    Unlike a plain ``compress_branch`` call, this never touches the branch's
    own authored commits — only sync noise sitting at the tip gets combined —
    so it's safe to run after every sync regardless of whether the branch has
    a published PR: a reviewer's "Files changed" tab diffs the PR's base
    against its current tip either way, unaffected by how many commits sit in
    between. A no-op (via ``compress_branch``'s own guard) when HEAD isn't
    currently a fresh merge.
    """
    branch = current_branch(root)
    parent = _parent_or_exit(branch, root=root)
    base = run(
        ["git", "merge-base", _parent_ref(parent, root=root), "HEAD"],
        cwd=root,
        capture_output=True,
    ).stdout.strip()
    anchor = _trailing_merge_anchor(base=base, root=root)
    message = f"Sync\n\n{_SYNC_COLLAPSE_TRAILER}: true"
    compress_branch(root=root, since=anchor, message=message, push=push)


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
    parent_ref = _parent_ref(parent, root=root)
    return ["git", *config_args, "diff", f"{parent_ref}...HEAD", *(extra_args or [])]


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
