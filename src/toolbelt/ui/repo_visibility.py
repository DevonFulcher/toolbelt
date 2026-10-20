"""Which registered repos the UI shows on this machine.

A repo whose GitHub owner is outside the employer orgs (``tasks.employer_orgs``,
see ``toolbelt.tasks.requirement``) is personal. Personal repos are shown only
when ``IS_PERSONAL=true`` (see ``toolbelt.personal_machine``), so a work laptop's
UI lists work repos alone. A repo without a github.com ``origin`` is treated as
work, as ``--task`` does.
"""

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack.store import RepoRecord
from toolbelt.personal_machine import is_personal
from toolbelt.tasks.config import employer_orgs
from toolbelt.tasks.requirement import github_owner


def visible_by_owner[T](
    items: Iterable[T],
    owner_of: Callable[[T], str | None],
    *,
    env: Mapping[str, str] = os.environ,
    config_file: Path | None = None,
) -> list[T]:
    """``items`` whose GitHub owner (``None``: unknown, treated as work) is an
    employer org; every item on a personal machine."""
    if is_personal(env):
        return list(items)
    orgs = employer_orgs(config_file or default_config_path())
    return [
        item for item in items if (owner := owner_of(item)) is None or owner in orgs
    ]


def visible_repos(
    repos: Iterable[RepoRecord],
    *,
    env: Mapping[str, str] = os.environ,
    config_file: Path | None = None,
) -> list[RepoRecord]:
    return visible_by_owner(
        repos,
        lambda repo: github_owner(repo.remote_url) if repo.remote_url else None,
        env=env,
        config_file=config_file,
    )
