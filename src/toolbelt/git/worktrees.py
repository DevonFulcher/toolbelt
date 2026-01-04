import os
import re
import shutil
import subprocess
from pathlib import Path

import typer

from toolbelt.editor import open_in_editor
from toolbelt.env_var import get_git_projects_workdir
from toolbelt.git.exec import capture, run
from toolbelt.git.commands import git_setup
from toolbelt.git.worktrees_ops import delete_branch_and_worktree
from toolbelt.logger import logger

worktrees_typer = typer.Typer(help="git worktree helpers")
WORKTREES_DIRNAME = "wt"


def repo_root() -> Path:
    try:
        return Path(capture(["git", "rev-parse", "--show-toplevel"]))
    except subprocess.CalledProcessError as err:
        logger.error("Error: not inside a Git repository.")
        raise typer.Exit(2) from err


def current_branch(root: Path) -> str:
    br = capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if br == "HEAD":
        logger.error("Error: detached HEAD.")
        raise typer.Exit(2)
    return br


def _repo_name_from_repo_root(repo_root: Path, git_projects_workdir: Path) -> str:
    """
    Derive a stable repo name for worktree namespacing.

    Prefer the repo root directory name, except when called from within a
    worktree path of the form:

        $GIT_PROJECTS_WORKDIR/wt/<repo>/<worktree>

    In that case, return the worktree's parent repo name (<repo>).
    """
    # Three cases:
    # - $GIT_PROJECTS_WORKDIR/worktrees/<repo>/<worktree>
    # - Repo is not under $GIT_PROJECTS_WORKDIR (fallback to repo_root.name)
    try:
        relative = repo_root.relative_to(git_projects_workdir)
    except ValueError:
        return repo_root.name

    match relative.parts:
        case (dirname, repo_name, *_) if dirname == WORKTREES_DIRNAME:
            return repo_name
        case _:
            # Defensive fallback; should be unreachable for existing path shapes.
            return repo_root.name


def get_worktrees_root(*, repo_root: Path) -> Path:
    git_projects_workdir = get_git_projects_workdir()
    repo_name = _repo_name_from_repo_root(repo_root, git_projects_workdir)
    return git_projects_workdir / WORKTREES_DIRNAME / repo_name


def _normalize_worktree_name(name: str) -> str:
    """
    Normalize a user-provided name into a safe worktree directory / branch suffix.

    Be lenient: prefer transforming over erroring. This replaces common unsafe
    path and branch characters with underscores.
    """
    normalized = name.strip()
    normalized = normalized.replace("\\", "/")
    normalized = normalized.replace("/", "_")
    normalized = normalized.replace(" ", "_")
    normalized = normalized.replace("..", "_")
    # Replace any remaining unsafe characters (keep common safe set).
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        raise typer.BadParameter("Invalid worktree name")
    return normalized


def _branch_name_for_worktree_name(name: str) -> str:
    return f"devon/{_normalize_worktree_name(name)}"


def _worktree_path_for_name(*, name: str, repo_root: Path) -> Path:
    worktrees_root = get_worktrees_root(repo_root=repo_root)
    worktrees_root.mkdir(parents=True, exist_ok=True)
    return worktrees_root / _normalize_worktree_name(name)


def _setup_new_worktree(*, root: Path, wt_path: Path) -> None:
    # Keep parity with `add` and avoid failing when these are absent.
    env_file = root / ".env"
    if env_file.exists():
        shutil.copy(env_file, wt_path / ".env")
    envrc_file = root / ".envrc"
    if envrc_file.exists():
        shutil.copy(envrc_file, wt_path / ".envrc")

    git_projects_workdir = get_git_projects_workdir()
    git_setup(wt_path, git_projects_workdir, index_serena=False)

    src_cache = root / ".serena/cache"
    dest_cache = wt_path / ".serena/cache"
    if src_cache.exists():
        dest_cache.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_cache, dest_cache, dirs_exist_ok=True)

    setup_script = wt_path / ".setup.sh"
    if setup_script.exists():
        if os.access(setup_script, os.X_OK):
            subprocess.run([str(setup_script)], check=True)
        else:
            logger.warning("Skipping '.setup.sh'; file is not executable.")


def _has_uncommitted_changes(*, root: Path) -> bool:
    return bool(capture(["git", "status", "--porcelain"], cwd=root))


def _create_stash(*, root: Path, message: str) -> str:
    run(
        ["git", "stash", "push", "-u", "-m", message],
        cwd=root,
        exit_on_error=True,
    )
    stash_ref = capture(["git", "stash", "list", "-1", "--format=%gd"], cwd=root)
    if not stash_ref:
        logger.error("Failed to create git stash.")
        raise typer.Exit(1)
    return stash_ref


@worktrees_typer.command()
def add(
    name: str = typer.Argument(..., help="Name of the new worktree"),
) -> None:
    """Create $GIT_PROJECTS_WORKDIR/wt/<repo>/<n>."""
    root = repo_root()
    # Branch name includes devon/ prefix, but path does not
    branch_name = _branch_name_for_worktree_name(name)
    wt_path = _worktree_path_for_name(name=name, repo_root=root)
    if wt_path.exists():
        logger.error(f"Error: worktree path already exists: {wt_path}")
        raise typer.Exit(1)

    start_ref = current_branch(root)
    cmd = ["git", "worktree", "add"]
    cmd += ["-b", branch_name]
    cmd += [str(wt_path), start_ref]

    logger.info(" ".join(cmd))
    run(cmd, cwd=root, exit_on_error=True)
    _setup_new_worktree(root=root, wt_path=wt_path)
    logger.info(f"Created worktree at {wt_path}")
    open_in_editor(wt_path)


@worktrees_typer.command()
def append(
    name: str = typer.Argument(..., help="Name of the new stacked worktree"),
) -> None:
    """
    Create a new stacked branch via git-town and open it in a new worktree.

    This carries over any uncommitted changes into the new worktree.
    """
    root = repo_root()

    orig_branch = current_branch(root)
    new_branch = _branch_name_for_worktree_name(name)
    wt_path = _worktree_path_for_name(name=name, repo_root=root)
    if wt_path.exists():
        logger.error(f"Error: worktree path already exists: {wt_path}")
        raise typer.Exit(1)

    # git-town append checks out the new branch in the current worktree.
    run(
        ["git-town", "append", new_branch],
        cwd=root,
        exit_on_error=True,
    )

    stash_ref: str | None = None
    try:
        if _has_uncommitted_changes(root=root):
            stash_ref = _create_stash(
                root=root,
                message=f"toolbelt wt append {new_branch}",
            )

        # Switch back so we can check out the new branch in the new worktree.
        run(["git", "checkout", orig_branch], cwd=root, exit_on_error=True)

        cmd = ["git", "worktree", "add", str(wt_path), new_branch]
        run(cmd, cwd=root, exit_on_error=True)

        if stash_ref is not None:
            run(["git", "stash", "apply", stash_ref], cwd=wt_path, exit_on_error=True)
            run(
                ["git", "stash", "drop", stash_ref],
                cwd=wt_path,
                exit_on_error=True,
            )

        _setup_new_worktree(root=root, wt_path=wt_path)
        logger.info(f"Created worktree at {wt_path}")
        open_in_editor(wt_path)
    finally:
        # Best-effort: ensure we leave the original worktree on the branch
        # the user started on, even if later steps fail.
        run(["git", "checkout", orig_branch], cwd=root, check=False)


@worktrees_typer.command()
def sync(
    stack: bool = typer.Option(
        False,
        "--stack",
        help="Sync the entire stack (git-town --stack).",
    ),
) -> None:
    """Sync the current worktree's branch using git-town (respects repo config)."""
    root = repo_root()
    # Non-fatal when there is nothing to continue.
    run(["git-town", "continue"], cwd=root, check=False)
    cmd = ["git-town", "sync"]
    if stack:
        cmd.append("--stack")
    run(cmd, cwd=root, exit_on_error=True)


def get_worktrees() -> list[str]:
    """Get list of worktree names."""
    root = repo_root()
    worktrees_dir = get_worktrees_root(repo_root=root)
    if not worktrees_dir.exists():
        return []
    return [d.name for d in worktrees_dir.iterdir() if d.is_dir()]


@worktrees_typer.command()
def remove(
    name: str | None = typer.Argument(
        None,
        help="Worktree name (with or without devon/ prefix)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Pass --force to git worktree remove.",
    ),
) -> None:
    """Remove $GIT_PROJECTS_WORKDIR/wt/<repo>/<n> and its branch."""
    if name is None:
        worktrees = get_worktrees()
        if not worktrees:
            logger.error("No worktrees found")
            raise typer.Exit(1)

        try:
            # Use subprocess.run directly for fzf since we need to pipe input
            proc = subprocess.run(
                ["fzf"],
                input="\n".join(worktrees).encode(),
                capture_output=True,
                check=True,
            )
            name = proc.stdout.decode().strip()
        except subprocess.CalledProcessError as err:
            logger.error("No worktree selected")
            raise typer.Exit(1) from err

    root = repo_root()
    delete_branch_and_worktree(name, repo_root=root, force=force)


@worktrees_typer.command(name="list")
def list_worktrees() -> None:
    """List worktrees."""
    root = repo_root()
    logger.info(capture(["git", "worktree", "list"], cwd=root))
