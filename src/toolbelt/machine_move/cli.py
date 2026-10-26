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
from toolbelt.machine_move.export_archive import ExportRequest, export_machine
lazy from toolbelt.git.stack.store import default_db_path
lazy from toolbelt.machine_move.import_archive import ImportRequest, import_machine
lazy from toolbelt.machine_move.one_password import OpCli
lazy from toolbelt.machine_move.service import LaunchdService

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
    push_branches: Annotated[
        bool,
        typer.Option(
            "--push-branches/--no-push-branches",
            help="Push the branches only this laptop has (open PRs and branches "
            "with an upstream; never by force; stale ones are left alone).",
        ),
    ] = True,
    stop_service: Annotated[
        bool,
        typer.Option(
            "--stop-service/--keep-service",
            help="Stop the tt ui service and its webhook forwarders before the DB "
            "is copied, so nothing changes after the snapshot and the new laptop "
            "can take over the webhooks.",
        ),
    ] = True,
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
        push_branches=push_branches,
        stop_service=stop_service,
    )
    # The service serves the DB of this laptop's toolbelt: an export of another
    # state directory (a test, a copy) must not stop it.
    service = (
        LaunchdService(home=resolved_home)
        if request.db_path == default_db_path()
        else None
    )
    try:
        report = export_machine(request, OpCli(vault=vault), service)
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
    if report.service_note:
        typer.echo(f"Service: {report.service_note}")
    for push in report.pushed:
        typer.echo(f"Push: {push}")
    for warning in report.warnings:
        typer.echo(f"WARNING: {warning}", err=True)
    if report.ignored_not_carried:
        typer.echo("Ignored by git and not carried (add to CARRIED_FILES if needed):")
        for entry in report.ignored_not_carried:
            typer.echo(f"  {entry}")
    to_do = [item for item in report.in_flight if not item.informational]
    if to_do:
        typer.echo(
            "\nWARNING: work that git does not carry. Commit and push each item "
            "(or accept losing it) before this laptop is wiped:",
            err=True,
        )
        for item in to_do:
            typer.echo(f"  [ ] {item.repo}: {item}", err=True)
    for item in report.in_flight:
        if item.informational:
            typer.echo(f"Not carried: {item.repo}: {item}")


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


@machine_typer.command(name="setup")
def setup_command(
    worktrees: Annotated[
        bool,
        typer.Option(
            "--worktrees",
            help="Also set up every worktree of those repos (a virtualenv each: "
            "slow and large; by default only the repos' own checkouts).",
        ),
    ] = False,
    state_dir: Annotated[Path | None, _STATE] = None,
    home: Annotated[Path | None, _HOME] = None,
) -> None:
    """Install the tool versions and Python environment of the restored repos

    Runs `asdf install` and `uv sync` in the checkout of each repo toolbelt
    tracks (where the checkout has a .tool-versions or a uv.lock). A failure is
    listed and the rest still runs. Safe to run again."""
    from toolbelt.git.stack.store import StackStore
    from toolbelt.machine_move.in_flight import worktrees as git_worktrees
    from toolbelt.machine_move.setup_repos import set_up

    resolved_home = _home(home)
    db_path = _state_dir(state_dir, resolved_home) / "stacks.db"
    if not db_path.is_file():
        typer.echo("error: there is no toolbelt DB to read the repos from.", err=True)
        raise typer.Exit(1)
    store = StackStore(db_path)
    try:
        repos = [repo.path for repo in store.repos()]
    finally:
        store.close()
    checkouts = list(repos)
    if worktrees:
        for repo in repos:
            checkouts += [tree.path for tree in git_worktrees(repo)[1:]]
    failed = False
    for result in set_up(checkouts):
        mark = "ok" if result.ok else "FAILED"
        typer.echo(
            f"{mark}: {result.path}: {result.command}"
            + (f" ({result.detail})" if result.detail else "")
        )
        failed = failed or not result.ok
    if failed:
        raise typer.Exit(1)
