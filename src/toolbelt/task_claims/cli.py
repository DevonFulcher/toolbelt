"""``tt task``: claim, release and inspect task claims (docs/task-claims.md).

Only ``typer`` is imported here at module level, so registering the group
costs ``tt`` nothing at start-up; the commands import the rest when they run.
The clock, the directories and the worktree lister are fields of
``TaskEnvironment``, so tests build the group with fakes."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer


@dataclass(frozen=True)
class TaskEnvironment:
    now: Callable[[], datetime]
    claims_dir: Callable[[], Path]
    db_path: Callable[[], Path]
    list_worktrees: Callable[[Path], dict[Path, str]]
    user: Callable[[], str]


def _real_environment() -> TaskEnvironment:
    import getpass

    from toolbelt.git.stack.store import default_db_path, state_dir
    from toolbelt.task_claims.claim_file import CLAIMS_DIRNAME
    from toolbelt.task_claims.resolution import git_worktrees

    return TaskEnvironment(
        now=lambda: datetime.now(UTC).replace(microsecond=0),
        claims_dir=lambda: state_dir() / CLAIMS_DIRNAME,
        db_path=default_db_path,
        list_worktrees=git_worktrees,
        user=getpass.getuser,
    )


def build_task_typer(environment: Callable[[], TaskEnvironment]) -> typer.Typer:
    task_typer = typer.Typer(help="Task claims: who is working on which task")

    def tracked() -> list[Any]:
        from toolbelt.git.stack.store import StackStore
        from toolbelt.task_claims.resolution import tracked_tasks

        env = environment()
        store = StackStore(env.db_path())
        try:
            return tracked_tasks(store, env.list_worktrees)
        finally:
            store.close()

    @task_typer.command(help="Claim tasks as a person (they show as 'With an agent').")
    def claim(
        refs: Annotated[
            list[str],
            typer.Argument(
                help="Branch names, task refs or task ids, or a worktree directory."
            ),
        ],
        ttl: Annotated[
            str, typer.Option(help="How long the claim lives: 90s, 30m, 8h.")
        ] = "8h",
        name: Annotated[
            str | None, typer.Option(help="Holder name (default: your user).")
        ] = None,
    ):
        from toolbelt.task_claims import operations
        from toolbelt.task_claims.resolution import resolve_claim

        env = environment()
        now = env.now()
        try:
            lifetime = operations.parse_duration(ttl)
            holder = operations.human_holder(name or env.user())
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
        tasks = tracked()
        claims = []
        for ref in refs:
            directory = Path(ref).expanduser()
            subject: str | Path = directory.resolve() if directory.is_dir() else ref
            claim_ = operations.make_claim(subject, now=now)
            matches = resolve_claim(claim_, tasks)
            if not matches:
                typer.echo(f"tt task: {ref!r} is not a tracked task.", err=True)
                raise typer.Exit(1)
            claims.append(claim_)
            for match in matches:
                typer.echo(f"claimed {match.repo_name}:{match.branch}")
        operations.write_human_claims(
            env.claims_dir(),
            name=holder.removeprefix(operations.HUMAN_PREFIX),
            claims=claims,
            ttl=lifetime,
            now=now,
        )

    @task_typer.command(
        help="Release claims (revokes an agent's; --force when it is working)."
    )
    def release(
        refs: Annotated[
            list[str] | None,
            typer.Argument(
                help="Branch names, task refs, task ids or worktree directories."
            ),
        ] = None,
        holder: Annotated[
            str | None, typer.Option(help="Release everything this holder has.")
        ] = None,
        all_: Annotated[
            bool, typer.Option("--all", help="Release every live holder's claims.")
        ] = False,
        force: Annotated[
            bool,
            typer.Option(
                "--force",
                help="Also revoke an agent that is working and heartbeating.",
            ),
        ] = False,
    ):
        from toolbelt.task_claims import operations
        from toolbelt.task_claims.reading import read_claims
        from toolbelt.task_claims.resolution import resolve_claim

        chosen = [bool(refs), holder is not None, all_]
        if sum(chosen) != 1:
            raise typer.BadParameter("give refs, or --holder, or --all (exactly one)")
        env = environment()
        now = env.now()
        reading = read_claims(env.claims_dir(), now)
        tasks = tracked()
        if holder is not None:
            if not any(h.holder == holder for h in reading.live):
                typer.echo(f"tt task: no live holder {holder!r}.", err=True)
                raise typer.Exit(1)
            select: operations.Selector = lambda h, _claim: h.holder == holder  # noqa: E731
        elif all_:
            select = lambda _h, _claim: True  # noqa: E731
        else:
            targets: set[tuple[int, str]] = set()
            literals: set[str] = set()
            for ref in refs or []:
                directory = Path(ref).expanduser()
                subject: str | Path = directory.resolve() if directory.is_dir() else ref
                literals.add(ref)
                targets.update(
                    task.key
                    for task in resolve_claim(
                        operations.make_claim(subject, now=now), tasks
                    )
                )
            select = operations.select_by_resolution(targets, literals, tasks)
        outcome = operations.release_claims(
            env.claims_dir(), reading, select=select, force=force, now=now
        )
        for released in outcome.released:
            typer.echo(f"{released.how} {released.holder}: {_subject(released.claim)}")
        for refused in outcome.refused:
            typer.echo(
                f"refused {refused.holder}: {_subject(refused.claim)}: {refused.reason}",
                err=True,
            )
        if outcome.refused:
            raise typer.Exit(1)
        if not outcome.released:
            typer.echo("tt task: nothing to release.", err=True)
            raise typer.Exit(1)

    @task_typer.command(help="Show who holds what.")
    def status(
        verbose: Annotated[
            bool,
            typer.Option(
                "--verbose",
                "-v",
                help="Also expired holders, unresolved claims, ignored files.",
            ),
        ] = False,
        as_json: Annotated[
            bool, typer.Option("--json", help="Machine-readable.")
        ] = False,
    ):
        from toolbelt.task_claims.reading import read_claims
        from toolbelt.task_claims.status_report import status_document, status_text

        env = environment()
        now = env.now()
        document = status_document(read_claims(env.claims_dir(), now), tracked(), now)
        if as_json:
            typer.echo(json.dumps(document, indent=2))
        else:
            typer.echo(status_text(document, verbose=verbose))

    @task_typer.command(help="Delete expired claim files and orphaned revocations.")
    def gc():
        from toolbelt.task_claims import operations
        from toolbelt.task_claims.reading import read_claims

        env = environment()
        now = env.now()
        outcome = operations.collect_garbage(
            env.claims_dir(), read_claims(env.claims_dir(), now), now
        )
        deleted = [*outcome.claim_files, *outcome.revocation_files, *outcome.temp_files]
        for name in deleted:
            typer.echo(f"deleted {name}")
        if not deleted:
            typer.echo("nothing to delete")

    return task_typer


def _subject(claim: Any) -> str:
    return f"{claim.kind} {claim.path if claim.kind == 'worktree' else claim.ref}"


task_typer = build_task_typer(_real_environment)
