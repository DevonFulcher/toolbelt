import subprocess

import typer

from toolbelt.datadog_form import form as datadog_form
from toolbelt.git.cli import git_typer
from toolbelt.mcp_server import mcp_typer
from toolbelt.repos import current_repo
from toolbelt.standup import standup_notes
from toolbelt.zsh import zsh_typer

# Create Typer app instances
app = typer.Typer(help="A collection of tools that I use.")
app.add_typer(git_typer, name="git")
app.add_typer(zsh_typer, name="zsh")
app.add_typer(mcp_typer, name="mcp")


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
        print("No unit tests configured for this repo")


@app.command(name="datadog", help="Datadog form")
def datadog():
    """Open Datadog form"""
    datadog_form()


@app.command(name="standup", help="Prepare notes for standup")
def standup():
    """Prepare notes for standup"""
    standup_notes()


if __name__ == "__main__":
    app()
