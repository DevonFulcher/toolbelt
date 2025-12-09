import os
import subprocess

import typer

from toolbelt.agent.cli import agent_typer
from toolbelt.datadog_form import form as datadog_form
from toolbelt.git.cli import git_typer
from toolbelt.github import display_status
from toolbelt.logger import logger
from toolbelt.mcp_server import mcp_typer
from toolbelt.repos import current_repo
from toolbelt.standup import standup_notes
from toolbelt.zsh import zsh_typer

# Create Typer app instances
app = typer.Typer(help="A collection of tools that I use.")
app.add_typer(git_typer, name="git")
app.add_typer(zsh_typer, name="zsh")
app.add_typer(mcp_typer, name="mcp")
app.add_typer(agent_typer, name="agent")


@app.command()
def upgrade():
    """Upgrade toolbelt"""
    subprocess.run(
        [
            "uv",
            "tool",
            "upgrade",
            "--reinstall",
            "toolbelt",
        ],
        check=True,
    )


@app.command()
def unit():
    """Run unit tests for this repo"""
    repo = current_repo()
    if repo:
        repo.check()
    else:
        logger.info("No unit tests configured for this repo")


@app.command(name="datadog", help="Datadog form")
def datadog():
    """Open Datadog form"""
    datadog_form()


@app.command(name="standup", help="Prepare notes for standup")
def standup():
    """Prepare notes for standup"""
    standup_notes()


@app.command(name="status", help="Show GitHub PR status")
def status():
    """Show your PRs and review requests with status information"""
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")

    if not username or not token:
        logger.error(
            "Error: GITHUB_USERNAME and GITHUB_PERSONAL_ACCESS_TOKEN environment variables must be set"
        )
        return

    display_status(username, token)


if __name__ == "__main__":
    app()
