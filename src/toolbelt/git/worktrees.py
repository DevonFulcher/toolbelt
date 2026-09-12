import os
import re
import shutil
import subprocess
from pathlib import Path

import typer

from toolbelt.env_var import get_git_projects_workdir
from toolbelt.git.constants import GIT_BRANCH_PREFIX
from toolbelt.git.exec import capture, run
from toolbelt.logger import logger

WORKTREES_DIRNAME = "wt"

# Dotfiles never worth copying into a new worktree: .git is managed per-worktree
# by git, and .venv is a build artifact whose scripts bake in absolute paths —
# it is rebuilt fresh by `uv sync`.
DOTFILES_COPY_EXCLUDE = {".git", ".venv"}


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
    return f"{GIT_BRANCH_PREFIX}{_normalize_worktree_name(name)}"


def _copy_file_if_present(src: Path, dest: Path) -> None:
    try:
        shutil.copy(src, dest)
    except FileNotFoundError:
        return


def _worktree_path_for_name(*, name: str, repo_root: Path) -> Path:
    worktrees_root = get_worktrees_root(repo_root=repo_root)
    worktrees_root.mkdir(parents=True, exist_ok=True)
    return worktrees_root / _normalize_worktree_name(name)


def copy_dotfiles(*, root: Path, wt_path: Path) -> None:
    # Mirror the repo root's dotfiles/dot-directories into the new worktree.
    # Regular files are copied so each worktree's config is independent (editing
    # one branch's .env must not affect siblings). Symlinks are preserved as
    # symlinks so dotfiles that intentionally point at a shared/global source
    # (e.g. cursor rules) stay live rather than being frozen into copies.
    for src in root.glob(".*"):
        if src.name in DOTFILES_COPY_EXCLUDE:
            continue
        dest = wt_path / src.name
        if src.is_symlink():
            dest.unlink(missing_ok=True)
            os.symlink(os.readlink(src), dest)
        elif src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)
        else:
            _copy_file_if_present(src, dest)


def _has_uncommitted_changes(*, root: Path) -> bool:
    return bool(capture(["git", "status", "--porcelain"], cwd=root))


def _commit_uncommitted(*, root: Path) -> None:
    if not _has_uncommitted_changes(root=root):
        return
    run(["git", "add", "-A"], cwd=root, exit_on_error=True)
    run(["git", "commit", "-m", "WIP"], cwd=root, exit_on_error=True)
