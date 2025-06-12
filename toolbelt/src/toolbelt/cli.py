import subprocess
from pathlib import Path
from typing import Optional, List

import typer
from typing_extensions import Annotated

from toolbelt.datadog_form import form as datadog_form
from toolbelt.git import git_pr, get_branch_name, git_safe_pull, git_fix
from toolbelt.repos import current_repo
from toolbelt.env_var import get_env_var_or_exit, get_git_projects_workdir
from toolbelt.git import git_setup, is_git_repo, git_save

import os
import re
import subprocess
from pathlib import Path

# Create Typer app instances
app = typer.Typer(help="A collection of tools that I use.")
app.add_typer(git_app, name="git")

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


if __name__ == "__main__":
    app()