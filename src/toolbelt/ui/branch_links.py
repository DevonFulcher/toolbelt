"""How a branch's stored task / agent-session links are shown.

Pure functions, so link rendering is testable without a web app.

A task ref is shown through the configured ``TaskTracker`` (see
``toolbelt.tasks``); with no tracker configured, or a ref that belongs to a
different tracker than the configured one, it is plain text.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from toolbelt.tasks.tracker import TaskTracker

# Schemes that execute or embed content in the page rather than navigate, so
# they are never rendered as an anchor even though the DB is the user's own.
_UNSAFE_SCHEMES = {"javascript", "data", "vbscript"}


@dataclass(frozen=True)
class Link:
    """``href`` is ``None`` when the value cannot (or should not) be linked."""

    label: str
    href: str | None


def _scheme(value: str) -> str:
    return urlsplit(value).scheme.lower()


def owning_tracker(
    stored_tracker: str | None, configured: TaskTracker | None
) -> TaskTracker | None:
    """``configured`` if a ref stored under ``stored_tracker`` belongs to it.
    Refs stored before trackers were recorded (``None``) are assumed to."""
    if configured is None:
        return None
    if stored_tracker is None or stored_tracker == configured.name:
        return configured
    return None


def task_link(ref: str | None, *, tracker: TaskTracker | None) -> Link | None:
    """``tracker`` is the one that owns ``ref`` (see ``owning_tracker``)."""
    if not ref:
        return None
    if tracker is None:
        return Link(label=ref, href=None)
    href = tracker.url_for(ref)
    if href is not None and _scheme(href) not in ("http", "https"):
        href = None
    return Link(label=tracker.display_ref(ref), href=href)


def agent_link(value: str | None) -> Link | None:
    """A deeplink to a Claude Desktop session; any non-executing scheme
    (``claude://``, ``https://``) is linked."""
    if not value:
        return None
    if not _scheme(value) or _scheme(value) in _UNSAFE_SCHEMES:
        return Link(label=value, href=None)
    return Link(label="Open session", href=value)
