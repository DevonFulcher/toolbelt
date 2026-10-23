import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from toolbelt import invocation_log, logged_process
from toolbelt.dotfiles.cli import dotfiles_typer
from toolbelt.git.cli import git_typer
from toolbelt.logger import logger
from toolbelt.task_claims.cli import task_typer
from toolbelt.zsh import zsh_typer
lazy from toolbelt.github.status import display_status
lazy from toolbelt.repos import current_repo
lazy from toolbelt.standup import parse_standup_weekdays, standup_notes

# Create Typer app instances
app = typer.Typer(help="A collection of tools that I use.")
app.add_typer(git_typer, name="git")
app.add_typer(git_typer, name="g", help="Alias for 'git'")
app.add_typer(zsh_typer, name="zsh")
app.add_typer(dotfiles_typer, name="dotfiles")
app.add_typer(task_typer, name="task")

slack_typer = typer.Typer(
    help="Slack: link messages posted by hand to their PRs.", no_args_is_help=True
)
app.add_typer(slack_typer, name="slack")


@slack_typer.command(name="link")
def slack_link(
    message_url: Annotated[
        str,
        typer.Argument(
            help="Link to the Slack message (https://<workspace>.slack.com/archives/<channel>/p<digits>)."
        ),
    ],
    pr: Annotated[
        list[str],
        typer.Option(
            "--pr",
            help="A GitHub PR URL the message is about (repeat for several).",
        ),
    ],
    ask: Annotated[
        str | None,
        typer.Option(
            "--ask",
            help="Who the message asked: a name from slack.reviewers (recorded with "
            "their Slack ID) or any other name, recorded as given (default: nobody).",
        ),
    ] = None,
):
    """Link a Slack message you posted by hand to the PRs (tasks) it is about

    Name the PRs with --pr and who was asked with --ask: toolbelt reads nothing
    from Slack, it records the message as the post of each PR. The PRs then read
    as posted: their post age counts from the message and the person asked is
    their Assigned Review. A message that is a reply in a thread is recorded as a
    bump. A running `tt ui` updates its cards on its own."""
    from toolbelt.git.repo_pull import default_config_path
    from toolbelt.git.stack.store import StackStore, default_db_path
    from toolbelt.ui.slack_config import slack_config
    from toolbelt.ui.slack_link import LinkError, link_message

    config = slack_config(default_config_path())
    if config is None:
        typer.echo("error: there is no slack: section in the config file.", err=True)
        raise typer.Exit(1)
    store = StackStore(default_db_path())
    try:
        rows = link_message(store, config, message_url=message_url, pr_urls=pr, ask=ask)
    except LinkError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from error
    finally:
        store.close()
    for row in rows:
        typer.echo(
            f"Linked {row.repo}#{row.pr_number} as a {row.kind} "
            f"(asked: {row.assignee_name or 'nobody'})"
        )


decisions_typer = typer.Typer(
    help="Decisions: questions agents ask you to answer.", no_args_is_help=True
)
app.add_typer(decisions_typer, name="decisions")


@decisions_typer.command(name="mcp")
def decisions_mcp():
    """Serve the decisions MCP server on stdio, for an interactive Claude session

    Register it once with:

        claude mcp add --scope user toolbelt-decisions -- toolbelt decisions mcp

    A session then asks you to decide with propose_decisions: the question shows
    in Claude (an MCP App) and in `tt ui` as the task's Deciding state, and the
    call waits for your answer. read_decisions lets any session read a task's
    questions and answers, even after the session that asked has closed."""
    from toolbelt.decisions.mcp_server import build_server

    build_server().run()


ui_typer = typer.Typer(
    help="Serve a read-only web view of every known repo's stacks on 127.0.0.1",
    invoke_without_command=True,
)
app.add_typer(ui_typer, name="ui")

DEFAULT_UI_PORT = 8765


@ui_typer.command(name="open")
def ui_open(
    port: int = typer.Option(
        DEFAULT_UI_PORT, "--port", "-p", help="The port `tt ui` listens on."
    ),
    view: str = typer.Option(
        "", "--view", help="Open this view (e.g. spend, aggregate, priorities)."
    ),
):
    """Open the running `tt ui` in the browser"""
    from toolbelt.ui.server import open_in_browser

    url = open_in_browser(port=port, view=view)
    typer.echo(url)


@ui_typer.callback()
def ui(
    ctx: typer.Context,
    port: int = typer.Option(
        DEFAULT_UI_PORT, "--port", "-p", help="Port to listen on (0 picks a free one)."
    ),
    build: bool = typer.Option(
        True,
        "--build/--no-build",
        help="Build the web client first when it is missing or out of date "
        "(needs node and pnpm).",
    ),
    log_file: Annotated[
        Path | None,
        typer.Option(
            "--log-file",
            help="Write the log here instead of the terminal, rotating it at "
            "10 MB and keeping three older files.",
        ),
    ] = None,
):
    """Serve a read-only web view of every known repo's stacks on 127.0.0.1"""
    if ctx.invoked_subcommand is not None:
        return
    # Imported here so the web stack doesn't slow down every other command.
    from toolbelt.git.stack import lineage
    from toolbelt.git.stack.cli import _repo_root_or_none
    from toolbelt.ui.client_build import ClientBuildError, build_client
    from toolbelt.ui.server import serve

    if build:
        try:
            build_client(say=lambda message: typer.echo(message, err=True))
        except ClientBuildError as error:
            typer.echo(f"tt ui: {error}", err=True)
            raise typer.Exit(1) from error
    cwd_root = _repo_root_or_none()
    if cwd_root is not None:
        # Same as `tt g tree`: make sure the current repo is registered.
        lineage.all_parents(root=cwd_root)
    if log_file is not None:
        from toolbelt.logger import log_to_rotating_file

        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_to_rotating_file(log_file)
    serve(port=port, cwd_root=cwd_root)


@app.command()
def spend(
    days: int = typer.Option(
        7, "--days", help="Days covered by the by-model and by-task breakdowns."
    ),
):
    """Show token spend from Claude Code's session transcripts (this machine)."""
    from toolbelt.token_spend.report import run_spend_report

    run_spend_report(days=days)


@app.command()
def upgrade():
    """Pull the toolbelt checkout, reinstall it, rebuild the web client and restart the `tt ui` service"""
    from toolbelt.ui.client_build import WEB_DIR, ClientBuildError, build_client
    from toolbelt.ui.launch_agent import restart_ui_launch_agent

    logged_process.run(
        ["git", "-C", str(WEB_DIR.parent), "pull", "--ff-only"], check=True
    )
    logged_process.run(
        [
            "uv",
            "tool",
            "upgrade",
            "--reinstall",
            "toolbelt",
        ],
        check=True,
    )
    try:
        build_client(force=True, say=typer.echo)
    except ClientBuildError as error:
        # The upgrade itself succeeded; `tt ui` retries the build.
        typer.echo(
            f"toolbelt upgraded, but the web client was not built: {error}", err=True
        )
    restart_ui_launch_agent(typer.echo)


@app.command()
def unit():
    """Run unit tests for this repo"""
    repo = current_repo()
    if repo:
        repo.unit()
    else:
        logger.info("No unit tests configured for this repo")


@app.command(name="logs")
def logs(
    lines: int = typer.Option(50, "--lines", "-n", help="Number of records to show."),
    invocation: str | None = typer.Option(
        None,
        "--invocation",
        "-i",
        help="Only records whose inv or parent_inv matches this invocation id.",
    ),
    path: bool = typer.Option(False, "--path", help="Print the log file path only."),
):
    """Show the tail of the persistent JSON-lines invocation log"""
    if path:
        typer.echo(invocation_log.log_path())
        return
    for line in invocation_log.read_records(invocation=invocation)[-lines:]:
        typer.echo(line)


@app.command(name="standup", help="Prepare notes for standup")
def standup(
    days: str = typer.Option(
        "tue,thu",
        "--days",
        help=("Standup weekdays. Examples: 'tue,thu'."),
    ),
):
    """Prepare notes for standup"""
    asyncio.run(standup_notes(standup_weekdays=parse_standup_weekdays(days)))


@app.command(name="status", help="Show GitHub PR status")
def status():
    """Show your PRs and review requests with status information"""
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")

    if not username or not token:
        logger.error(
            "Error: GITHUB_USERNAME and GITHUB_PERSONAL_ACCESS_TOKEN environment "
            "variables must be set"
        )
        return

    asyncio.run(display_status(username, token))


if __name__ == "__main__":
    app()
