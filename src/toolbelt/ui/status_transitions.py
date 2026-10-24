"""Records when a branch's conflict, CI and open-comments status change, as
the status refresher observes them (``status_transitions`` table).

GitHub has no event for a PR becoming conflicting, for review threads opening
or resolving, or for a CI rollup moving between running, failing and passing,
so the card timeline can only draw those spans from what ``tt ui`` itself saw. A value is recorded when it differs from
the last one recorded for the branch; the first lookup of a branch records its
baseline at that moment.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from toolbelt.git.stack.status import BranchStatus, PrState
from toolbelt.git.stack.store import StackStore, default_db_path

#: The dimensions recorded, with their value read from a lookup. ``conflict``
#: is only meaningful for an open PR (``none`` otherwise), like the card's Merge.
CONFLICT = "conflict"
CI = "ci"
COMMENTS = "comments"


def observed(status: BranchStatus) -> dict[str, str]:
    """The recorded dimensions' values for a lookup."""
    if status.pr in (PrState.OPEN, PrState.DRAFT):
        conflict = "conflicts" if status.has_conflicts else "clean"
    else:
        conflict = "none"
    comments = (
        "open" if status.pr is not PrState.NONE and status.open_comments else "none"
    )
    return {CONFLICT: conflict, CI: status.ci.value, COMMENTS: comments}


class StatusTransitionRecorder:
    """Called from worker threads (it opens its own store, like the lookup
    cache)."""

    def __init__(self, db_path: Callable[[], Path] = default_db_path) -> None:
        self._db_path = db_path

    def observe(
        self, repo_id: int, branch: str, status: BranchStatus, at: datetime
    ) -> bool:
        """Record what changed; returns whether anything did."""
        store = StackStore(self._db_path())
        try:
            changed = False
            for dimension, value in observed(status).items():
                changed |= store.record_status_transition(
                    repo_id, branch, dimension, value, at
                )
            return changed
        finally:
            store.close()
