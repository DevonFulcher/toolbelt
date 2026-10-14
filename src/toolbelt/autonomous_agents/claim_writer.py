"""An autonomous run's claim file (contract v2, ``docs/task-claims.md``): how
the rest of toolbelt, and every other agent, knows a run holds the task."""

import json
from datetime import datetime
from pathlib import Path

from toolbelt.task_claims.atomic_file import write_atomically
from toolbelt.task_claims.claim_file import AUTONOMOUS_VERSION, iso

HOLDER_PREFIX = "auto."
#: The heartbeat keeps a live claim alive this long; the writer rewrites it
#: well within it.
CLAIM_TTL_SECONDS = 180
HEARTBEAT_SECONDS = 30


def holder_name(run_id: str) -> str:
    return f"{HOLDER_PREFIX}{run_id}"


class RunClaim:
    """The claim file of one run: written when the run starts, rewritten as a
    heartbeat while it works, and ended by rewriting it already expired (the
    contract's way of releasing a claim)."""

    def __init__(self, claims_dir: Path, run_id: str, worktree: Path) -> None:
        self._path = claims_dir / f"{holder_name(run_id)}.json"
        self._run_id = run_id
        self._worktree = worktree
        self._started_at: datetime | None = None

    def _write(
        self, now: datetime, *, ttl_seconds: int, state: str, claims: list[dict]
    ) -> None:
        assert self._started_at is not None
        document = {
            "version": AUTONOMOUS_VERSION,
            "holder": holder_name(self._run_id),
            "kind": "autonomous",
            "session_id": None,
            "agent_id": None,
            "link": None,
            "run_id": self._run_id,
            "state": state,
            "started_at": iso(self._started_at),
            "updated_at": iso(now),
            "ttl_seconds": ttl_seconds,
            "claims": claims,
        }
        write_atomically(self._path, json.dumps(document, indent=2))

    def start(self, now: datetime) -> None:
        self._started_at = now
        self.heartbeat(now)

    def heartbeat(self, now: datetime) -> None:
        self._write(
            now,
            ttl_seconds=CLAIM_TTL_SECONDS,
            state="working",
            claims=[
                {
                    "kind": "worktree",
                    "path": str(self._worktree),
                    "touched_at": iso(now),
                }
            ],
        )

    def end(self, now: datetime) -> None:
        self._write(now, ttl_seconds=0, state="idle", claims=[])
