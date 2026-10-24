"""`tt machine`: export this laptop's non-git state, import it on another."""

import os
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from toolbelt.machine_move.archive_layout import (
    DEFAULT_DOCUMENT_TITLE,
    MachineMoveError,
)
lazy from toolbelt.machine_move.export_archive import ExportRequest, export_machine
lazy from toolbelt.machine_move.import_archive import ImportRequest, import_machine
lazy from toolbelt.machine_move.one_password import OpCli

machine_typer = typer.Typer(
    help="Move to a new laptop: export on the old one, import on the new one",
    no_args_is_help=True,
)

_TITLE = typer.Option(
    "--title", help="Name of the 1Password document holding the archive."
)
_VAULT = typer.Option("--vault", help="1Password vault (default: the CLI's default).")


def _home(home: Path | None) -> Path:
    return home or Path.home()


def _workdir(workdir: Path | None, home: Path) -> Path:
    if workdir:
        return workdir
    from_env = os.environ.get("GIT_PROJECTS_WORKDIR")
    return Path(from_env) if from_env else home / "git"


def _state_dir(state_dir: Path | None, home: Path) -> Path:
    if state_dir:
        return state_dir
    override = os.environ.get("TOOLBELT_HOME")
    return Path(override) if override else home / ".toolbelt"


_HOME = typer.Option("--home", hidden=True, help="Home directory.")
_WORKDIR = typer.Option("--workdir", hidden=True, help="Git projects directory.")
_STATE = typer.Option("--state-dir", hidden=True, help="toolbelt's state directory.")


@machine_typer.command(name="export")
def export_command(
    to_file: Annotated[
        Path | None,
        typer.Option(
            "--to-file",
            help="Write the archive here instead of storing it in 1Password.",
        ),
    ] = None,
    title: Annotated[str, _TITLE] = DEFAULT_DOCUMENT_TITLE,
    vault: Annotated[str | None, _VAULT] = None,
    include_history: Annotated[
        bool, typer.Option("--include-history", help="Also carry ~/.zsh_history.")
    ] = False,
    ssh_key: Annotated[
        list[Path] | None,
        typer.Option(
            "--ssh-key",
            help="Carry this key (and its .pub) besides the signing and GitHub keys.",
        ),
    ] = None,
    no_ssh_keys: Annotated[
        bool, typer.Option("--no-ssh-keys", help="Carry no SSH keys or ssh config.")
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Export even while a run is active, a decision is pending, or "
            "machine.sh says IS_PERSONAL=true.",
        ),
    ] = False,
    home: Annotated[Path | None, _HOME] = None,
    workdir: Annotated[Path | None, _WORKDIR] = None,
    state_dir: Annotated[Path | None, _STATE] = None,
) -> None:
    """Put everything git does not carry into one archive (1Password or a file)

    Carries the gitignored files of the dotfiles repos and toolbelt (secrets/*,
    including machine.sh), toolbelt's DB (copied with sqlite's backup; the
    machine-specific tables emptied), the SSH signing/GitHub keys, and Claude
    memory files. Lists uncommitted and unpushed work and stashes: git does not
    carry them. See docs/machine-move.md."""
    resolved_home = _home(home)
    request = ExportRequest(
        home=resolved_home,
        workdir=_workdir(workdir, resolved_home),
        db_path=_state_dir(state_dir, resolved_home) / "stacks.db",
        title=title,
        to_file=to_file,
        include_history=include_history,
        ssh_keys=None if no_ssh_keys else tuple(ssh_key or ()),
        force=force,
    )
    try:
        report = export_machine(request, OpCli(vault=vault))
    except MachineMoveError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from error

    typer.echo(
        f"Archived {len(report.members)} file(s)"
        + (" and the toolbelt DB" if report.has_database else "")
        + ":"
    )
    for member in report.members:
        typer.echo(f"  {member.root}/{member.path}")
    typer.echo(f"Stored: {report.destination}")
    for warning in report.warnings:
        typer.echo(f"WARNING: {warning}", err=True)
    if report.ignored_not_carried:
        typer.echo("Ignored by git and not carried (add to CARRIED_FILES if needed):")
        for entry in report.ignored_not_carried:
            typer.echo(f"  {entry}")
    if report.in_flight:
        typer.echo(
            "\nWARNING: work that git does not carry. Commit and push each item "
            "(or accept losing it) before this laptop is wiped:",
            err=True,
        )
        for item in report.in_flight:
            typer.echo(f"  [ ] {item.repo}: {item}", err=True)


@machine_typer.command(name="import")
def import_command(
    from_file: Annotated[
        Path | None,
        typer.Option(
            "--from-file", help="Read the archive from here instead of 1Password."
        ),
    ] = None,
    title: Annotated[str, _TITLE] = DEFAULT_DOCUMENT_TITLE,
    vault: Annotated[str | None, _VAULT] = None,
    clone_missing: Annotated[
        bool,
        typer.Option(
            "--clone-missing/--no-clone-missing",
            help="Clone the repos toolbelt tracks that are missing here.",
        ),
    ] = True,
    replace_db: Annotated[
        bool,
        typer.Option(
            "--replace-db",
            help="Replace this laptop's toolbelt DB even if it already tracks repos "
            "(it is backed up first).",
        ),
    ] = False,
    home: Annotated[Path | None, _HOME] = None,
    workdir: Annotated[Path | None, _WORKDIR] = None,
    state_dir: Annotated[Path | None, _STATE] = None,
) -> None:
    """Restore an archive made by `tt machine export` on this laptop

    Restores the files (SSH keys with mode 600), the toolbelt DB with the old
    home directory rewritten to this one, clones tracked repos that are missing
    and recreates worktrees whose branch is on origin. Safe to run again."""
    resolved_home = _home(home)
    try:
        with tempfile.TemporaryDirectory(prefix="toolbelt-machine-archive-") as tmp:
            archive = from_file
            if archive is None:
                archive = Path(tmp) / "machine-archive.zip"
                vault_client = OpCli(vault=vault)
                vault_client.ensure_signed_in()
                vault_client.fetch(title, to=archive)
            report = import_machine(
                ImportRequest(
                    archive=archive,
                    home=resolved_home,
                    workdir=_workdir(workdir, resolved_home),
                    state_dir=_state_dir(state_dir, resolved_home),
                    clone_missing=clone_missing,
                    replace_db=replace_db,
                )
            )
    except MachineMoveError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from error
    for line in report.done:
        typer.echo(line)
    if report.follow_ups:
        typer.echo("\nStill to do:", err=True)
        for follow_up in report.follow_ups:
            typer.echo(f"  [ ] {follow_up}", err=True)
