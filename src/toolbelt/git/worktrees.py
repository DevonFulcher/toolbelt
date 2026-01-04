import os
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

        $GIT_PROJECTS_WORKDIR/worktrees/<repo>/<worktree>

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


@worktrees_typer.command()
def add(
    name: str = typer.Argument(..., help="Name of the new worktree"),
) -> None:
    """Create $GIT_PROJECTS_WORK_DIR/worktrees/<repo>/<n>."""
    root = repo_root()
    # Branch name includes devon/ prefix, but path does not
    branch_name = f"devon/{name.replace(' ', '_')}"
    path_name = name.replace(" ", "_")
    worktrees_root = get_worktrees_root(repo_root=root)
    worktrees_root.mkdir(parents=True, exist_ok=True)
    wt_path = worktrees_root / path_name

    start_ref = current_branch(root)
    cmd = ["git", "worktree", "add"]
    cmd += ["-b", branch_name]
    cmd += [str(wt_path), start_ref]

    logger.info(" ".join(cmd))
    run(cmd, cwd=root)
    shutil.copy(root / ".env", wt_path / ".env")
    shutil.copy(root / ".envrc", wt_path / ".envrc")

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
    logger.info(f"Created worktree at {wt_path}")
    open_in_editor(wt_path)


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
    """Remove $GIT_PROJECTS_WORK_DIR/worktrees/<repo>/<n> and its branch."""
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
