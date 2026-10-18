"""Which registered repos the UI shows on this machine.

A repo whose GitHub owner is outside the employer orgs (``tasks.employer_orgs``,
see ``toolbelt.tasks.requirement``) is personal. Personal repos are shown only
when ``IS_PERSONAL=true`` (see ``toolbelt.personal_machine``), so a work laptop's
UI lists work repos alone. A repo without a github.com ``origin`` is treated as
work, as ``--task`` does.
"""

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack.store import RepoRecord
from toolbelt.personal_machine import is_personal
from toolbelt.tasks.config import employer_orgs
from toolbelt.tasks.requirement import github_owner


def visible_repos(
    repos: Iterable[RepoRecord],
    *,
    env: Mapping[str, str] = os.environ,
    config_file: Path | None = None,
) -> list[RepoRecord]:
    if is_personal(env):
        return list(repos)
    orgs = employer_orgs(config_file or default_config_path())
    visible = []
    for repo in repos:
        owner = github_owner(repo.remote_url) if repo.remote_url else None
        if owner is None or owner in orgs:
            visible.append(repo)
    return visible
