"""Which task tracker to use, from ``tasks`` in toolbelt's config file.

``$XDG_CONFIG_HOME/toolbelt/config.yaml`` (see
``toolbelt.git.repo_pull.default_config_path``)::

    tasks:
      tracker: jira                     # omit for no tracker: refs are plain text
      jira:
        base_url: https://example.atlassian.net
        email: me@example.com           # basic auth only
        auth: basic                     # basic (Cloud, default) | bearer (Server/DC)
        api_version: 3                  # 2 for Server / Data Center
        token_command: [op, read, op://Private/Jira/token]

The API token is never kept in this file; see ``toolbelt.tasks.jira``.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from toolbelt.tasks.jira import JiraTracker
from toolbelt.tasks.tracker import TaskTracker


def configured_tracker(
    config_file: Path, *, env: Mapping[str, str] = os.environ, **jira_kwargs: Any
) -> TaskTracker | None:
    """The tracker named by ``tasks.tracker``, or ``None`` if unset.

    ``jira_kwargs`` (``run_command``, ``client``) are passed to
    ``JiraTracker`` so tests can inject fakes."""
    if not config_file.exists():
        return None
    tasks = (yaml.safe_load(config_file.read_text()) or {}).get("tasks") or {}
    name = tasks.get("tracker")
    if not name:
        return None
    if name == "jira":
        return JiraTracker.from_config(tasks.get("jira") or {}, env=env, **jira_kwargs)
    raise ValueError(f"tasks.tracker: unknown tracker {name!r} (supported: jira)")
