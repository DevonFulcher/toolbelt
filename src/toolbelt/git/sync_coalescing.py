"""Let a sync (or fetch) that another command already ran stand in for this one.

``sync`` converges the whole stack toward a state that depends only on the repo
when the run starts, so a run that *started after* a caller asked for one has
seen everything that caller wanted synced and makes a second run redundant.
Callers waiting behind it for the stack lock then return without repeating the
work; whatever the run hit (a conflict, a rejected push) is not recorded, so
they repeat it and hit the same thing themselves.

"Started after" is decided with tickets rather than clocks: a caller takes a
ticket (a per-repo counter in the stack database, bumped in one transaction, so
it only grows) once its own changes are in the repo, and a run records the
counter's value when it begins. A run's value is at least a ticket exactly when
the ticket was taken before the run began, with no reliance on clocks agreeing
or not stepping backwards.

A run covers a caller only when it finished successfully and, if the caller
asked for ``--merge-base-branch``, itself merged the base.
"""

from collections.abc import Callable
from pathlib import Path

from toolbelt.git.repo_lock import named_mutex
from toolbelt.git.stack import lineage
from toolbelt.git.stack.store import SyncRun

_FETCH_MUTEX = "fetch"
FETCH_SCOPE = "fetch"
BRANCH_CLEAN_SCOPE = "branch-clean"


class SyncLedger:
    """Tickets, and the last successful run per scope, for one repo.

    A scope is whatever one lock serializes: a stack (see ``stack_scope``), or
    ``BRANCH_CLEAN_SCOPE`` / ``FETCH_SCOPE`` for the repo-wide steps. Reading
    and writing a scope's record must happen while holding that scope's lock.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def take_ticket(self) -> int:
        """A new ticket: after every run that began before this call, and
        before every run that begins after it."""
        return lineage.take_sync_ticket(root=self._root)

    def current_ticket(self) -> int:
        """The newest ticket handed out so far (0 if none): what a run that is
        starting now records as its ``started_at``."""
        return lineage.current_sync_ticket(root=self._root)

    def covers(
        self, scope: str, *, ticket: int, merge_base_branch: bool = False
    ) -> bool:
        """Whether the last successful run in ``scope`` began after ``ticket``
        was taken and did everything the caller asked for."""
        last = lineage.get_sync_run(scope, root=self._root)
        if last is None:
            return False
        if last.started_at < ticket:
            return False
        return last.merge_base_branch or not merge_base_branch

    def record(self, scope: str, run: SyncRun) -> None:
        """Remember ``run`` as ``scope``'s last successful run."""
        lineage.record_sync_run(scope, run, root=self._root)


def fetch_once(root: Path, *, fetch: Callable[[], object]) -> None:
    """Run ``fetch`` (a ``git fetch -p``) unless one that began after this call
    already finished.

    Fetches serialize on a mutex, and one that waits behind another finds that
    one's result covers it: the remote-tracking refs it updated are at least as
    new as this call asked for.
    """
    ledger = SyncLedger(root)
    ticket = ledger.take_ticket()
    with named_mutex(root, _FETCH_MUTEX):
        if ledger.covers(FETCH_SCOPE, ticket=ticket):
            return
        started_at = ledger.current_ticket()
        fetch()
        ledger.record(
            FETCH_SCOPE, SyncRun(started_at=started_at, merge_base_branch=False)
        )
