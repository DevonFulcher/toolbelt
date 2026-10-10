"""The interface every task tracker implements.

A branch stores a raw task reference (a key like ``ABC-123``, or a URL) and the
name of the tracker it belongs to. Everything that shows a reference goes
through a ``TaskTracker``, so no caller needs to know which tracker it is.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TaskInfo:
    ref: str
    title: str
    status: str
    url: str | None


class TaskTracker(Protocol):
    #: Stored with each branch's task ref (``task_tracker``) and matched
    #: against ``tasks.tracker`` in the config.
    name: str

    def url_for(self, ref: str) -> str | None:
        """Where ``ref`` can be opened, or ``None`` if it cannot be linked.
        Never touches the network."""
        ...

    def display_ref(self, ref: str) -> str:
        """A short label for ``ref`` (e.g. the key out of a full URL)."""
        ...

    def unavailable_reason(self) -> str | None:
        """A one-line reason ``fetch`` cannot work at all (e.g. credentials
        not set), or ``None`` if it can. Never touches the network."""
        ...

    def fetch(self, ref: str) -> TaskInfo | None:
        """The task's title and status, or ``None`` if they cannot be
        obtained (unreachable, unauthorized, unknown ref). Never raises for
        those; this is for display only."""
        ...
