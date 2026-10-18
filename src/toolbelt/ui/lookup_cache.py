"""The last good GitHub lookup of each tracked branch's PR, kept in the stack
DB (``pr_lookups``).

The status refresher saves a branch's lookup whenever the full lookup ran, and
loads them all when the server starts. So a restart (or a stretch when GitHub
refuses requests) leaves the cards showing what was last known, with the real
age of that data, instead of nothing. The ETags the gate had for the PR are
saved with it: they are what lets the restarted server ask GitHub "did it
change?" for free instead of repeating every lookup.

A row whose ``schema`` is not ``RECORD_SCHEMA`` is ignored (the branch is
looked up again).
"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from toolbelt.git.stack.status import (
    BranchStatus,
    CiState,
    PrState,
    ReviewState,
)
from toolbelt.git.stack.store import PrLookupRow, StackStore, default_db_path
from toolbelt.ui.conflict_commit import ConflictCommit
from toolbelt.ui.pr_activity import PrFacts, facts_from_dict, facts_to_dict

RECORD_SCHEMA = 1


@dataclass(frozen=True)
class SavedLookup:
    """A branch's last good lookup. ``fetched_at`` is when GitHub was last
    asked (wall clock) and ``full_at`` when the full lookup last ran."""

    status: BranchStatus
    facts: PrFacts | None
    conflict: ConflictCommit | None
    fetched_at: datetime
    full_at: datetime
    #: The gate's ETags for the PR, by REST path.
    etags: dict[str, str]


def _record_json(saved: SavedLookup) -> str:
    return json.dumps(
        {
            "status": asdict(saved.status),
            "facts": facts_to_dict(saved.facts) if saved.facts else None,
            "conflict": asdict(saved.conflict) if saved.conflict else None,
            "etags": saved.etags,
        },
        separators=(",", ":"),
    )


def _status(data: dict) -> BranchStatus:
    return BranchStatus(
        **{
            **data,
            "pr": PrState(data["pr"]),
            "ci": CiState(data["ci"]),
            "review": ReviewState(data["review"]),
            "assignees": tuple(data["assignees"]),
            "reviewers": tuple(data["reviewers"]),
        }
    )


def _conflict(data: dict) -> ConflictCommit:
    return ConflictCommit(**{**data, "files": tuple(data["files"])})


def _saved(row: PrLookupRow) -> SavedLookup:
    data = json.loads(row.record)
    return SavedLookup(
        status=_status(data["status"]),
        facts=facts_from_dict(data["facts"]) if data["facts"] else None,
        conflict=_conflict(data["conflict"]) if data["conflict"] else None,
        fetched_at=row.fetched_at,
        full_at=row.full_at,
        etags=data["etags"],
    )


class LookupCache:
    """``load`` / ``save`` / ``delete`` over the stack DB; each opens its own
    connection, so they can run in a worker thread."""

    def __init__(self, db_path: Callable[[], Path] = default_db_path) -> None:
        self._db_path = db_path

    def load(self) -> dict[tuple[int, str], SavedLookup]:
        store = StackStore(self._db_path())
        try:
            return {
                (row.repo_id, row.branch): _saved(row)
                for row in store.pr_lookups()
                if row.schema == RECORD_SCHEMA
            }
        finally:
            store.close()

    def save(self, repo_id: int, branch: str, saved: SavedLookup) -> None:
        store = StackStore(self._db_path())
        try:
            store.save_pr_lookup(
                PrLookupRow(
                    repo_id=repo_id,
                    branch=branch,
                    fetched_at=saved.fetched_at,
                    full_at=saved.full_at,
                    record=_record_json(saved),
                    schema=RECORD_SCHEMA,
                )
            )
        finally:
            store.close()

    def delete(self, repo_id: int, branch: str) -> None:
        store = StackStore(self._db_path())
        try:
            store.delete_pr_lookup(repo_id, branch)
        finally:
            store.close()
