import os
import shutil
import subprocess
from pathlib import Path

import typer

from toolbelt.editor import open_in_editor
from toolbelt.env_var import get_git_projects_workdir
from toolbelt.git.commands import (
    delete_branch_and_worktree,
    git_safe_pull,
    git_setup,
    update_repo,
)
from toolbelt.logger import logger

worktrees_typer = typer.Typer(help="git worktree helpers")


def run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def capture(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.check_output(cmd, cwd=str(cwd) if cwd else None).decode().strip()


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


def get_worktrees_root() -> Path:
    return get_git_projects_workdir() / "worktrees"


@worktrees_typer.command()
def add(
    name: str = typer.Argument(..., help="Name of the new worktree"),
) -> None:
    """Create $GIT_PROJECTS_WORK_DIR/worktrees/<n>."""
    root = repo_root()
    # Branch name includes devon/ prefix, but path does not
    branch_name = f"devon/{name.replace(' ', '_')}"
    path_name = name.replace(" ", "_")
    worktrees_root = get_worktrees_root()
    wt_path = worktrees_root / path_name
    worktrees_root.mkdir(parents=True, exist_ok=True)

    start_ref = current_branch(root)
    cmd = ["git", "worktree", "add"]
    cmd += ["-b", branch_name]
    cmd += [str(wt_path), start_ref]

    logger.info("$ " + " ".join(cmd))
    run(cmd, cwd=root)
    git_projects_workdir = get_git_projects_workdir()
    git_setup(wt_path, git_projects_workdir, index_serena=False)
    shutil.copy(root / ".serena/cache", wt_path / ".serena/cache")
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
    worktrees_dir = get_worktrees_root()
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
    """Remove $GIT_PROJECTS_WORK_DIR/worktrees/<n> and its branch."""
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


@worktrees_typer.command()
def change(
    name: str | None = typer.Argument(None, help="Worktree name to change to"),
) -> None:
    """Change to a worktree and switch to a branch."""
    repo_root()

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

    wt_path = get_worktrees_root() / name
    if not wt_path.exists():
        logger.error(f"Worktree {name} does not exist")
        raise typer.Exit(1)

    # Get current branch and switch to it, then safe pull
    current_br = current_branch(wt_path)
    run(["git", "checkout", current_br], cwd=wt_path)

    git_safe_pull()
    update_repo(wt_path)

    # Output the directory path for shell integration
    logger.info(f"cd {wt_path}")


@worktrees_typer.command(name="list")
def list_worktrees() -> None:
    """List worktrees."""
    root = repo_root()
    logger.info(capture(["git", "worktree", "list"], cwd=root))
