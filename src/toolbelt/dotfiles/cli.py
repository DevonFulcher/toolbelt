"""`tt dotfiles`: link dotfiles repos into the home directory."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import typer

from toolbelt.dotfiles.actions import (
    Action,
    BackupDir,
    backup_actions,
    link_actions,
    run_actions,
    unlink_actions,
)
from toolbelt.dotfiles.adopt import adopt_actions
from toolbelt.dotfiles.config import config_path, dotfiles_repos
from toolbelt.dotfiles.manifest import RepoManifest, load_manifest
from toolbelt.dotfiles.plan import Plan, build_plan
from toolbelt.dotfiles.secret_scan import scan_repo
from toolbelt.dotfiles.status import State, collect_status

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
    home: Path | None = typer.Option(
        None, "--home", hidden=True, help="Home directory to manage."
    ),
    config: Path | None = typer.Option(
        None, "--config", hidden=True, help="Path to toolbelt's config.yaml."
    ),
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


@dotfiles_typer.command()
def unlink(ctx: typer.Context, dry_run: bool = _DRY_RUN) -> None:
    """Remove links that point into the repos (copies are backed up)"""
    context = _context(ctx)
    _execute(unlink_actions(context.plan()), home=context.home, dry_run=dry_run)


@dotfiles_typer.command()
def backup(ctx: typer.Context, dry_run: bool = _DRY_RUN) -> None:
    """Copy aside every file `link` would displace, changing nothing else"""
    context = _context(ctx)
    _execute(backup_actions(context.plan()), home=context.home, dry_run=dry_run)


@dotfiles_typer.command()
def adopt(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="Existing file or directory under home."),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="Target repo directory name or path. Default: first private repo.",
    ),
    src: str | None = typer.Option(
        None,
        "--src",
        help="Path inside the repo. Default: <adopt_dir>/<home-relative>.",
    ),
    dry_run: bool = _DRY_RUN,
) -> None:
    """Move an existing real file into a repo, record it, and link it back"""
    context = _context(ctx)
    manifests = context.manifests()
    chosen = _choose_repo(manifests, repo)
    actions = adopt_actions(
        path.expanduser().absolute(),
        repo=chosen,
        plan=build_plan(manifests, home=context.home),
        home=context.home,
        src_rel=src,
    )
    _execute(actions, home=context.home, dry_run=dry_run)


def _choose_repo(manifests: list[RepoManifest], repo: str | None) -> RepoManifest:
    if repo is None:
        private = [m for m in manifests if not m.public]
        if not private:
            raise ValueError("no private repo configured; pass --repo")
        return private[0]
    for manifest in manifests:
        if repo in (manifest.root.name, str(manifest.root)):
            return manifest
    raise ValueError(f"--repo {repo} is not one of the configured dotfiles repos")


@dotfiles_typer.command()
def check(
    ctx: typer.Context,
    repo: list[str] | None = typer.Argument(
        None, help="Repo names or paths. Default: every public repo."
    ),
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
