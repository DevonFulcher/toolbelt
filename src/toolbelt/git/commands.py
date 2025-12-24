"""
Compatibility façade for git-related commands.

Historically, CLI modules imported helpers from `toolbelt.git.commands`. Over time,
the implementations were organized into more focused modules (e.g. `workflow`,
`branches`, `repo`, `bootstrap.repo_setup`). We keep re-exports here so existing
imports continue to work (and so type-checking can resolve the symbols).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from toolbelt.bootstrap.repo_setup import git_setup
from toolbelt.git.branches import get_branch_name
from toolbelt.git.repo import get_current_repo_root_path
from toolbelt.git.workflow import (
    git_branch_clean,
    git_pr,
    git_safe_pull,
    git_save,
    sync_repo,
    update_repo,
)

__all__ = [
    "get_branch_name",
    "get_current_repo_root_path",
    "git_branch_clean",
    "git_pr",
    "git_safe_pull",
    "git_save",
    "git_setup",
    "is_git_repo",
    "sync_repo",
    "update_repo",
]


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(path),
        )
        return True
    except subprocess.CalledProcessError:
        return False
