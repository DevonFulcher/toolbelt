"""Let a sync (or fetch) that another command already ran stand in for this one.

``sync`` converges the whole stack toward a state that depends only on the repo
when the run starts, so a run that *started after* a caller asked for one has
seen everything that caller wanted synced and makes a second run redundant.
Callers waiting behind it for the stack lock then return without repeating the
work; whatever the run hit (a conflict, a rejected push) is not recorded, so
they repeat it and hit the same thing themselves.

"Started after" is decided with tickets rather than clocks: a caller takes a
ticket (a counter bumped under a mutex, persisted next to the lock files, so it
only grows) once its own changes are in the repo, and a run records the
counter's value when it begins. A run's value is at least a ticket exactly when
the ticket was taken before the run began, with no reliance on clocks agreeing
or not stepping backwards.

A run covers a caller only when it finished successfully and, if the caller
asked for ``--merge-base-branch``, itself merged the base.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from toolbelt.git.repo_lock import git_common_dir, named_mutex

_LEDGER_DIRNAME = "toolbelt-sync"
_TICKET_FILENAME = "ticket"
_TICKET_MUTEX = "ticket"
_FETCH_MUTEX = "fetch"
FETCH_SCOPE = "fetch"
BRANCH_CLEAN_SCOPE = "branch-clean"


@dataclass(frozen=True)
class FinishedRun:
    """A run that completed successfully."""

    #: The ticket counter's value when the run began.
    started_at: int
    merge_base_branch: bool


class SyncLedger:
    """Tickets, and the last successful run per scope, for one repo.

    A scope is whatever one lock serializes: a stack (see ``stack_scope``), or
    ``BRANCH_CLEAN_SCOPE`` / ``FETCH_SCOPE`` for the repo-wide steps. Reading
    and writing a scope's record must happen while holding that scope's lock;
    only the ticket counter has a mutex of its own.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._dir = git_common_dir(root) / _LEDGER_DIRNAME
        self._dir.mkdir(exist_ok=True)

    def take_ticket(self) -> int:
        """A new ticket: after every run that began before this call, and
        before every run that begins after it."""
        with named_mutex(self._root, _TICKET_MUTEX):
            ticket = self.current_ticket() + 1
            self._write_atomically(self._dir / _TICKET_FILENAME, str(ticket))
        return ticket

    def current_ticket(self) -> int:
        """The newest ticket handed out so far (0 if none): what a run that is
        starting now records as its ``started_at``."""
        path = self._dir / _TICKET_FILENAME
        if not path.exists():
            return 0
        return int(path.read_text())

    def covers(
        self, scope: str, *, ticket: int, merge_base_branch: bool = False
    ) -> bool:
        """Whether the last successful run in ``scope`` began after ``ticket``
        was taken and did everything the caller asked for."""
        last = self._last_run(scope)
        if last is None:
            return False
        if last.started_at < ticket:
            return False
        return last.merge_base_branch or not merge_base_branch

    def record(self, scope: str, run: FinishedRun) -> None:
        """Remember ``run`` as ``scope``'s last successful run."""
        payload = json.dumps(
            {
                "started_at": run.started_at,
                "merge_base_branch": run.merge_base_branch,
            }
        )
        self._write_atomically(self._dir / f"{scope}.json", payload)

    def _last_run(self, scope: str) -> FinishedRun | None:
        path = self._dir / f"{scope}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return FinishedRun(
            started_at=data["started_at"],
            merge_base_branch=data["merge_base_branch"],
        )

    @staticmethod
    def _write_atomically(path: Path, text: str) -> None:
        """Replace ``path`` in one step, so a reader never sees half of it."""
        scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        scratch.write_text(text)
        scratch.replace(path)


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
            FETCH_SCOPE, FinishedRun(started_at=started_at, merge_base_branch=False)
        )
