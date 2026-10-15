"""Whether a repo's branches must be tied to a task (``--task``).

A task is required for repos owned by an employer org (``tasks.employer_orgs``
in the config file, see ``toolbelt.tasks.config``), decided from the GitHub
owner of the repo's ``origin`` remote. Repos owned by anyone else are personal
and need no task. ``IS_PERSONAL=true`` (see ``toolbelt.personal_machine``)
waives the requirement everywhere. A missing remote, or one that is not a
github.com URL, keeps the requirement.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

from toolbelt import logged_process
from toolbelt.personal_machine import is_personal
from toolbelt.tasks.config import employer_orgs

# git@github.com:owner/repo(.git), ssh://git@github.com[:22]/owner/repo,
# https://[user@]github.com/owner/repo
_GITHUB_OWNER_RE = re.compile(
    r"^(?:(?:ssh|https?|git)://)?(?:[^@/]+@)?github\.com(?::\d+)?[:/]([^/:]+)/[^/]+$"
)


def github_owner(remote_url: str) -> str | None:
    """Lower-case owner of a github.com remote URL (ssh or https), else None."""
    match = _GITHUB_OWNER_RE.match(remote_url.strip())
    return match.group(1).lower() if match else None


def origin_owner(root: Path) -> str | None:
    result = logged_process.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return github_owner(result.stdout) if result.returncode == 0 else None


def task_required(
    root: Path, config_file: Path, *, env: Mapping[str, str] = os.environ
) -> bool:
    if is_personal(env):
        return False
    owner = origin_owner(root)
    return owner is None or owner in employer_orgs(config_file)
