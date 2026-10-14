"""`tt dotfiles`: link dotfiles repos into the home directory."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

lazy from toolbelt.dotfiles.actions import (
    Action,
    BackupDir,
    link_actions,
    run_actions,
)
lazy from toolbelt.dotfiles.config import config_path, dotfiles_repos
lazy from toolbelt.dotfiles.manifest import (
    MANIFEST_FILENAME,
    RepoManifest,
    load_manifest,
)
lazy from toolbelt.dotfiles.plan import Plan, build_plan
lazy from toolbelt.dotfiles.secret_scan import scan_repo
lazy from toolbelt.dotfiles.status import State, collect_status

dotfiles_typer = typer.Typer(
    help="Link dotfiles from the configured repos into the home directory"
)


@dataclass(frozen=True)
class Context:
    home: Path
    config_file: Path

    def manifests(self) -> list[RepoManifest]:
        return [
            load_manifest(root)
            for root in dotfiles_repos(self.config_file, home=self.home)
        ]

    def plan(self) -> Plan:
        return build_plan(self.manifests(), home=self.home)


@dotfiles_typer.callback()
def _callback(
    ctx: typer.Context,
    home: Annotated[
        Path | None,
        typer.Option("--home", hidden=True, help="Home directory to manage."),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", hidden=True, help="Path to toolbelt's config.yaml."),
    ] = None,
) -> None:
    resolved_home = home or Path.home()
    ctx.obj = Context(
        home=resolved_home,
        config_file=config or config_path(os.environ, home=resolved_home),
    )


def _context(ctx: typer.Context) -> Context:
    assert isinstance(ctx.obj, Context)
    return ctx.obj


def _execute(
    actions: list[Action],
    *,
    home: Path,
    dry_run: bool,
    now: Callable[[], datetime] = datetime.now,
) -> None:
    if not actions:
        typer.echo("Nothing to do.")
        return
    lines = run_actions(actions, backups=BackupDir(home, now), dry_run=dry_run)
    for line in lines:
        typer.echo(f"{'[dry-run] ' if dry_run else ''}{line}")


_DRY_RUN = typer.Option(
    False, "--dry-run", help="Show what would change; change nothing."
)


@dotfiles_typer.command()
def status(ctx: typer.Context) -> None:
    """Show whether each managed path is linked (read-only)"""
    context = _context(ctx)
    entries = collect_status(context.plan())
    for entry in entries:
        detail = f"  ({entry.detail})" if entry.detail else ""
        typer.echo(f"{entry.state.value:<15} {entry.dest}{detail}")
    problems = [e for e in entries if e.state is not State.OK]
    typer.echo(f"{len(entries) - len(problems)} ok, {len(problems)} need attention")
    if problems:
        raise typer.Exit(1)


@dotfiles_typer.command()
def link(ctx: typer.Context, dry_run: bool = _DRY_RUN) -> None:
    """Link everything in the manifests; displaced files are backed up first"""
    context = _context(ctx)
    _execute(link_actions(context.plan()), home=context.home, dry_run=dry_run)


def _choose_repo(manifests: list[RepoManifest], repo: str) -> RepoManifest:
    for manifest in manifests:
        if repo in (manifest.root.name, str(manifest.root)):
            return manifest
    # Not configured, but a directory with its own manifest (e.g. a git
    # worktree of a configured repo) can still be scanned.
    if (Path(repo) / MANIFEST_FILENAME).is_file():
        return load_manifest(Path(repo))
    raise ValueError(
        f"{repo} is neither a configured dotfiles repo nor a directory "
        f"containing {MANIFEST_FILENAME}"
    )


@dotfiles_typer.command()
def check(
    ctx: typer.Context,
    repo: Annotated[
        list[str] | None,
        typer.Argument(help="Repo names or paths. Default: every public repo."),
    ] = None,
) -> None:
    """Scan public repos for secret-looking content; exit 1 if any is found"""
    manifests = _context(ctx).manifests()
    if repo:
        selected = [_choose_repo(manifests, name) for name in repo]
    else:
        selected = [m for m in manifests if m.public]
    findings = [f for m in selected for f in scan_repo(m.root)]
    for finding in findings:
        typer.echo(str(finding))
    if findings:
        typer.echo(f"{len(findings)} possible secret(s) found")
        raise typer.Exit(1)
    typer.echo(f"No secrets found in {len(selected)} repo(s)")
