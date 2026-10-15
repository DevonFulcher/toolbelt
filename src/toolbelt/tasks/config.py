"""Which task tracker to use, from ``tasks`` in toolbelt's config file.

``$XDG_CONFIG_HOME/toolbelt/config.yaml`` (see
``toolbelt.git.repo_pull.default_config_path``)::

    tasks:
      tracker: jira                     # omit for no tracker: refs are plain text
      jira:
        base_url: https://example.atlassian.net
        email: me@example.com           # basic auth only; $JIRA_EMAIL wins
        auth: basic                     # basic (Cloud, default) | bearer (Server/DC)
        api_version: 3                  # 2 for Server / Data Center
      employer_orgs: [dbt-labs, fivetran]   # GitHub owners whose repos need a task

``tasks.employer_orgs`` defaults to ``dbt-labs`` and ``fivetran``. A repo whose
GitHub ``origin`` owner is in it needs ``--task`` on ``toolbelt git append``;
any other repo does not (see ``toolbelt.tasks.requirement``).

The API token is never kept in this file: it comes from ``$JIRA_API_TOKEN``
(see ``toolbelt.tasks.jira``).
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

lazy import yaml

from toolbelt.tasks.jira import JiraTracker
from toolbelt.tasks.tracker import TaskTracker

DEFAULT_EMPLOYER_ORGS = ("dbt-labs", "fivetran")


def employer_orgs(config_file: Path) -> tuple[str, ...]:
    """``tasks.employer_orgs`` (lower case), or ``DEFAULT_EMPLOYER_ORGS`` if unset."""
    if not config_file.exists():
        return DEFAULT_EMPLOYER_ORGS
    tasks = (yaml.safe_load(config_file.read_text()) or {}).get("tasks") or {}
    orgs = tasks.get("employer_orgs")
    if orgs is None:
        return DEFAULT_EMPLOYER_ORGS
    return tuple(str(org).lower() for org in orgs)


def configured_tracker(
    config_file: Path, *, env: Mapping[str, str] = os.environ, **jira_kwargs: Any
) -> TaskTracker | None:
    """The tracker named by ``tasks.tracker``, or ``None`` if unset.

    ``jira_kwargs`` (``client``) are passed to
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
