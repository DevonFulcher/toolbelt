"""How a branch's stored Jira / agent-session links are shown.

Pure functions, so link rendering is testable without a web app.

``jira.base_url`` in toolbelt's config file (see
``toolbelt.git.repo_pull.default_config_path``) is the Jira site root::

    jira:
      base_url: https://example.atlassian.net

A stored Jira *key* (``ABC-123``) links to ``<base_url>/browse/<key>``; with no
``base_url`` it is shown as plain text. A stored URL is linked as-is.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

# Schemes that execute or embed content in the page rather than navigate, so
# they are never rendered as an anchor even though the DB is the user's own.
_UNSAFE_SCHEMES = {"javascript", "data", "vbscript"}


@dataclass(frozen=True)
class Link:
    """``href`` is ``None`` when the value cannot (or should not) be linked."""

    label: str
    href: str | None


def jira_base_url(config_file: Path) -> str | None:
    """``jira.base_url`` from the YAML config at ``config_file``, if set."""
    if not config_file.exists():
        return None
    config = yaml.safe_load(config_file.read_text()) or {}
    value = (config.get("jira") or {}).get("base_url")
    return str(value) if value else None


def _scheme(value: str) -> str:
    return urlsplit(value).scheme.lower()


def jira_link(value: str | None, *, base_url: str | None) -> Link | None:
    if not value:
        return None
    if _scheme(value) in ("http", "https"):
        key = value.rstrip("/").rsplit("/", 1)[-1]
        label = key if re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", key) else value
        return Link(label=label, href=value)
    if base_url:
        return Link(label=value, href=f"{base_url.rstrip('/')}/browse/{value}")
    return Link(label=value, href=None)


def agent_link(value: str | None) -> Link | None:
    """A deeplink to a Claude Desktop session; any non-executing scheme
    (``claude://``, ``https://``) is linked."""
    if not value:
        return None
    if not _scheme(value) or _scheme(value) in _UNSAFE_SCHEMES:
        return Link(label=value, href=None)
    return Link(label="Open session", href=value)
