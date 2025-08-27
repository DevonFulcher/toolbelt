import subprocess
from pathlib import Path

import typer

worktrees_typer = typer.Typer(help="git worktree helpers")


def run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def capture(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.check_output(cmd, cwd=str(cwd) if cwd else None).decode().strip()


def repo_root() -> Path:
    try:
        return Path(capture(["git", "rev-parse", "--show-toplevel"]))
    except subprocess.CalledProcessError as err:
        typer.echo("Error: not inside a Git repository.", err=True)
        raise typer.Exit(2) from err


def current_branch(root: Path) -> str:
    br = capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if br == "HEAD":
        typer.echo(
            "Error: detached HEAD. Use --start <ref> or checkout a branch.", err=True
        )
        raise typer.Exit(2)
    return br


@worktrees_typer.command()
def add(
    name: str = typer.Argument(..., help="Name of the new worktree"),
) -> None:
    """Create ./worktrees/<n>."""
    root = repo_root()
    wt_path = root / "worktrees" / name
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    start_ref = current_branch(root)
    cmd = ["git", "worktree", "add"]
    cmd += ["-b", name]
    cmd += [str(wt_path), start_ref]

    typer.echo("$ " + " ".join(cmd))
    run(cmd, cwd=root)
    typer.echo(f"Created worktree at {wt_path}")


def get_worktrees() -> list[str]:
    """Get list of worktree names."""
    root = repo_root()
    worktrees_dir = root / "worktrees"
    if not worktrees_dir.exists():
        return []
    return [d.name for d in worktrees_dir.iterdir() if d.is_dir()]


@worktrees_typer.command()
def remove(
    name: str | None = typer.Argument(None, help="Worktree name"),
) -> None:
    """Remove ./worktrees/<n> and its branch."""
    if name is None:
        worktrees = get_worktrees()
        if not worktrees:
            typer.echo("No worktrees found", err=True)
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
            typer.echo("No worktree selected", err=True)
            raise typer.Exit(1) from err

    root = repo_root()
    wt_path = root / "worktrees" / name

    # Remove worktree
    cmd = ["git", "worktree", "remove", str(wt_path)]
    typer.echo("$ " + " ".join(cmd))
    run(cmd, cwd=root)
    typer.echo(f"Removed {wt_path}")

    # Delete branch
    cmd = ["git", "branch", "-D", name]
    typer.echo("$ " + " ".join(cmd))
    run(cmd, cwd=root)
    typer.echo(f"Deleted branch {name}")


@worktrees_typer.command(name="list")
def list_worktrees() -> None:
    """List worktrees."""
    root = repo_root()
    typer.echo(capture(["git", "worktree", "list"], cwd=root))
