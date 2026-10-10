import os
import subprocess
from pathlib import Path

from toolbelt import logged_process
from toolbelt.git.constants import GIT_BRANCH_PREFIX
from toolbelt.logger import logger


def main_worktree(root: Path) -> Path:
    """The repo's main working tree (first entry of ``git worktree list``).

    Branch/worktree deletion should run with this as ``repo_root``, even when
    the caller's own current worktree is the one being deleted: git refuses
    to remove a worktree the git process itself is running in, and a
    long-running process whose cwd is deleted out from under it can break in
    stranger ways than that on top.
    """
    result = logged_process.run(
        ["git", "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    )
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree ") :])
    return root


def _worktree_entries(root: Path) -> list[tuple[Path, str | None]]:
    """
    Return the registered git worktrees and their associated branch names.

    Parameters
    ----------
    root:
        Path to the repository root.
    """
    result = logged_process.run(
        ["git", "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    )

    entries: list[tuple[Path, str | None]] = []
    current_path: Path | None = None
    current_branch: str | None = None
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current_path is not None:
                entries.append((current_path, current_branch))
            current_path = None
            current_branch = None
            continue

        key, _, value = line.partition(" ")
        if key == "worktree":
            current_path = Path(value)
        elif key == "branch":
            current_branch = value.removeprefix("refs/heads/")
        elif key == "detached":
            current_branch = None

    if current_path is not None:
        entries.append((current_path, current_branch))

    return entries


def _worktree_paths_for_branch(branch_name: str, root: Path) -> list[Path]:
    """
    Return the list of worktree paths that are currently checked out to the
    provided branch.
    """
    paths = [
        path
        for path, branch in _worktree_entries(root)
        if branch is not None and branch == branch_name
    ]

    unique_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for path in paths:
        if path not in seen_paths:
            unique_paths.append(path)
            seen_paths.add(path)

    return unique_paths


def _is_worktree_dirty(path: Path) -> bool:
    """True if ``path``'s worktree has uncommitted or untracked changes."""
    result = logged_process.run(
        ["git", "status", "--porcelain"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def _remove_worktree(path: Path, *, force: bool) -> None:
    """Make ``path`` disappear as a worktree, deleting its files in the
    background rather than blocking on it.

    A worktree's files can number in the tens of thousands (a Python venv,
    `node_modules`, ...), and deleting that many small files can take upward
    of ten seconds — git-specific bookkeeping (checking cleanliness,
    unregistering) is comparatively instant. So instead of ``git worktree
    remove`` (one call that does both, blocking on the slow part), this
    replicates its cleanliness check (unless ``force``), then renames the
    directory out of the way — an instant same-filesystem rename regardless
    of file count — and deletes the renamed copy in a detached background
    process. The caller's ``git worktree prune`` right after this picks up
    the now-missing directory and drops its registration immediately.

    This doesn't honor `git worktree lock` the way `git worktree remove`
    does — not a concern today, since nothing in this codebase locks a
    worktree.
    """
    if not force and _is_worktree_dirty(path):
        message = (
            f"fatal: '{path}' contains modified or untracked files, use "
            "--force to delete it"
        )
        logger.error(message)
        raise subprocess.CalledProcessError(
            1, ["git", "worktree", "remove", str(path)], stderr=message
        )

    trash_path = path.with_name(f".toolbelt-trash-{path.name}-{os.getpid()}")
    path.rename(trash_path)
    logger.info(f"Removing {path} in the background...")
    logged_process.Popen(
        ["rm", "-rf", str(trash_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def delete_branch_and_worktree(
    branch_name: str,
    *,
    repo_root: Path,
    force: bool = False,
) -> str:
    """
    Delete a local branch and its associated worktree (if present).

    Parameters
    ----------
    branch_name:
        The name of the branch to delete. Resolved against both the
        prefixed and bare form (see ``candidates`` below), since callers may
        not know which one is actually checked out.
    repo_root:
        Path to the repository root.
    force:
        If True, skip the check for uncommitted/untracked changes.

    Returns
    -------
    The exact branch name that was deleted, after prefix resolution — use
    this (not the original ``branch_name`` argument) for any follow-up
    lookup keyed by branch name, e.g. ``lineage.remove_parent``.
    """
    root = repo_root
    branch_to_delete = branch_name

    def branch_exists(name: str) -> bool:
        result = logged_process.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
            cwd=root,
            check=False,
        )
        return result.returncode == 0

    candidates: list[str] = []
    candidates.append(branch_name)
    bare_name = branch_name.removeprefix(GIT_BRANCH_PREFIX)
    if branch_name == bare_name:
        candidates.append(f"{GIT_BRANCH_PREFIX}{bare_name}")
    else:
        candidates.append(bare_name)

    for candidate in candidates:
        if candidate and branch_exists(candidate):
            branch_to_delete = candidate
            break

    worktree_paths = _worktree_paths_for_branch(branch_to_delete, root)

    for path in worktree_paths:
        _remove_worktree(path, force=force)

    # Clean up any stale worktree references so branch deletion succeeds.
    logged_process.run(["git", "worktree", "prune"], check=True, cwd=root)

    logger.info(f"git branch -D {branch_to_delete}")
    branch_delete = logged_process.run(
        ["git", "branch", "-D", branch_to_delete],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    if branch_delete.stdout:
        logger.info(branch_delete.stdout.rstrip())
    if branch_delete.returncode != 0:
        if branch_delete.stderr:
            logger.error(branch_delete.stderr.rstrip())
        raise subprocess.CalledProcessError(
            branch_delete.returncode,
            branch_delete.args,
            output=branch_delete.stdout,
            stderr=branch_delete.stderr,
        )
    logger.info(f"Deleted branch {branch_to_delete}")
    return branch_to_delete
