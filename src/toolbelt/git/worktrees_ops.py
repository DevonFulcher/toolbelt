import subprocess
from pathlib import Path

from toolbelt.git.constants import GIT_BRANCH_PREFIX
from toolbelt.logger import logger


def _worktree_entries(root: Path) -> list[tuple[Path, str | None]]:
    """
    Return the registered git worktrees and their associated branch names.

    Parameters
    ----------
    root:
        Path to the repository root.
    """
    result = subprocess.run(
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


def delete_branch_and_worktree(
    branch_name: str,
    *,
    repo_root: Path,
    force: bool = False,
) -> None:
    """
    Delete a local branch and its associated worktree (if present).

    Parameters
    ----------
    branch_name:
        The name of the branch to delete.
    repo_root:
        Path to the repository root.
    force:
        If True, pass ``--force`` to ``git worktree remove``.
    """
    root = repo_root
    branch_to_delete = branch_name

    def branch_exists(name: str) -> bool:
        result = subprocess.run(
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
        cmd = ["git", "worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(str(path))
        logger.info(" ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=root,
            check=False,
        )
        if result.stdout:
            logger.info(result.stdout.rstrip())
        if result.returncode != 0:
            if result.stderr:
                logger.error(result.stderr.rstrip())
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
        logger.info(f"Removed {path}")

    # Clean up any stale worktree references so branch deletion succeeds.
    subprocess.run(["git", "worktree", "prune"], check=True, cwd=root)

    logger.info(f"git branch -D {branch_to_delete}")
    branch_delete = subprocess.run(
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
