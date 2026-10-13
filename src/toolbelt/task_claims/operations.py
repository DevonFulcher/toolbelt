"""What ``tt task`` does to the claims directory, as functions of an injected
directory and clock.

Safe from another process: every write is an atomic rename, an agent's own
file is never edited (``release`` writes a revocation it will honour), and
``gc`` re-reads a file right before deleting it so a holder that revived
between the scan and the delete is kept."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from toolbelt.task_claims.atomic_file import TEMP_SUFFIX, write_atomically
from toolbelt.task_claims.claim_file import (
    HOLDER_PATTERN,
    REVOKED_DIRNAME,
    SUPPORTED_VERSION,
    Claim,
    RefClaim,
    UnreadableFile,
    WorktreeClaim,
    claim_key,
    iso,
    parse_claim_file,
)
from toolbelt.task_claims.reading import Holder, Reading, classify
from toolbelt.task_claims.resolution import TrackedTask, resolve_claim
from toolbelt.task_claims.revocations import record_revocations

HUMAN_PREFIX = "human."
DEFAULT_HUMAN_TTL = timedelta(hours=8)
RELEASED_BY = "tt task release"
#: A leftover temp file of a writer that died mid-write.
_STALE_TEMP_AGE = timedelta(hours=1)

_DURATION = re.compile(r"^(\d+)([smhd]?)$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta:
    """``90`` or ``90s`` (seconds), ``30m``, ``8h``, ``2d``."""
    match = _DURATION.match(text.strip())
    if match is None or int(match.group(1)) == 0:
        raise ValueError(f"not a duration: {text!r} (use e.g. 90s, 30m, 8h)")
    return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])


def human_holder(name: str) -> str:
    holder = f"{HUMAN_PREFIX}{name}"
    if not HOLDER_PATTERN.match(holder):
        raise ValueError(
            f"{name!r} cannot be part of a holder id (letters, digits, '.', '_', '-')"
        )
    return holder


def claim_path(claims_dir: Path, holder: str) -> Path:
    return claims_dir / f"{holder}.json"


def _dump(file_document: dict) -> str:
    return json.dumps(file_document, indent=2) + "\n"


def _claim_document(claim: Claim) -> dict:
    body = {"path": claim.path} if claim.kind == "worktree" else {"ref": claim.ref}
    return {"kind": claim.kind, **body, "touched_at": iso(claim.touched_at)}


def _holder_document(
    *,
    holder: str,
    kind: str,
    state: str,
    started_at: datetime,
    updated_at: datetime,
    ttl_seconds: int,
    claims: list[Claim],
    session_id: str | None = None,
    agent_id: str | None = None,
    link: str | None = None,
) -> dict:
    return {
        "version": SUPPORTED_VERSION,
        "holder": holder,
        "kind": kind,
        "session_id": session_id,
        "agent_id": agent_id,
        "link": link,
        "state": state,
        "started_at": iso(started_at),
        "updated_at": iso(updated_at),
        "ttl_seconds": ttl_seconds,
        "claims": [_claim_document(claim) for claim in claims],
    }


def make_claim(subject: str | Path, *, now: datetime) -> Claim:
    """A worktree claim for an existing directory, a ref claim otherwise."""
    if isinstance(subject, Path):
        return WorktreeClaim(kind="worktree", path=str(subject), touched_at=now)
    return RefClaim(kind="ref", ref=subject, touched_at=now)


def write_human_claims(
    claims_dir: Path,
    *,
    name: str,
    claims: list[Claim],
    ttl: timedelta,
    now: datetime,
) -> str:
    """Adds ``claims`` to ``human.<name>.json`` (created if needed, working,
    live for ``ttl`` from ``now``). Returns the holder id. A claim already in
    the file is refreshed, not duplicated."""
    holder = human_holder(name)
    path = claim_path(claims_dir, holder)
    started_at = now
    kept: list[Claim] = []
    if path.is_file():
        try:
            existing = parse_claim_file(path.read_text(), expected_holder=holder)
        except UnreadableFile:
            existing = None  # tt owns human files: a broken one is replaced
        if existing is not None:
            started_at = existing.started_at
            kept = list(existing.claims)
    replaced = {claim_key(claim) for claim in claims}
    merged = [c for c in kept if claim_key(c) not in replaced] + claims
    write_atomically(
        path,
        _dump(
            _holder_document(
                holder=holder,
                kind="human",
                state="working",
                started_at=started_at,
                updated_at=now,
                ttl_seconds=int(ttl.total_seconds()),
                claims=merged,
            )
        ),
    )
    return holder


# ---- release ------------------------------------------------------------------------


@dataclass(frozen=True)
class Released:
    holder: str
    claim: Claim
    #: ``removed`` from a human file, or ``revoked`` through the revocation file.
    how: str


@dataclass(frozen=True)
class Refused:
    holder: str
    claim: Claim
    reason: str


@dataclass(frozen=True)
class ReleaseOutcome:
    released: list[Released] = field(default_factory=list)
    refused: list[Refused] = field(default_factory=list)


Selector = Callable[[Holder, Claim], bool]


def release_claims(
    claims_dir: Path,
    reading: Reading,
    *,
    select: Selector,
    force: bool,
    now: datetime,
    by: str = RELEASED_BY,
) -> ReleaseOutcome:
    """Releases the standing claims of live holders that ``select`` picks.

    A ``human.*`` file is edited (and deleted once it has no claims). An
    agent's file is left alone and a revocation is recorded for its claims.
    An agent that is ``working`` and heartbeating is refused unless ``force``
    (it is actively on the task); one that is stale, idle or blocked is not."""
    outcome = ReleaseOutcome()
    for holder in reading.live:
        picked = [claim for claim in holder.claims if select(holder, claim)]
        if not picked:
            continue
        if holder.file.kind == "human":
            _remove_human_claims(claims_dir, holder, picked)
            outcome.released.extend(
                Released(holder.holder, c, "removed") for c in picked
            )
            continue
        if holder.status == "working" and not holder.stale and not force:
            outcome.refused.extend(
                Refused(
                    holder.holder,
                    claim,
                    "the agent is working and heartbeating (use --force to revoke it)",
                )
                for claim in picked
            )
            continue
        record_revocations(claims_dir, holder.holder, picked, at=now, by=by)
        outcome.released.extend(Released(holder.holder, c, "revoked") for c in picked)
    return outcome


def _remove_human_claims(claims_dir: Path, holder: Holder, picked: list[Claim]) -> None:
    gone = {claim_key(claim) for claim in picked}
    remaining = [c for c in holder.file.claims if claim_key(c) not in gone]
    path = claim_path(claims_dir, holder.holder)
    if not remaining:
        path.unlink(missing_ok=True)
        return
    write_atomically(
        path,
        _dump(
            _holder_document(
                holder=holder.holder,
                kind="human",
                state=holder.file.state,
                started_at=holder.file.started_at,
                updated_at=holder.file.updated_at,
                ttl_seconds=holder.file.ttl_seconds,
                claims=remaining,
            )
        ),
    )


def select_by_resolution(
    targets: set[tuple[int, str]],
    literals: set[str],
    tasks: list[TrackedTask],
) -> Selector:
    """Picks claims that resolve to one of the ``targets`` (repo id, branch),
    or are a ref claim spelled exactly like one of ``literals`` (so a claim on
    a branch that is no longer tracked can still be released by name)."""

    def select(_holder: Holder, claim: Claim) -> bool:
        if claim.kind == "ref" and claim.ref in literals:
            return True
        return any(task.key in targets for task in resolve_claim(claim, tasks))

    return select


# ---- gc -------------------------------------------------------------------------------


@dataclass(frozen=True)
class GcOutcome:
    claim_files: list[str] = field(default_factory=list)
    revocation_files: list[str] = field(default_factory=list)
    temp_files: list[str] = field(default_factory=list)


def collect_garbage(claims_dir: Path, reading: Reading, now: datetime) -> GcOutcome:
    """Deletes expired holder files, revocation files with no live holder, and
    temp files a dead writer left behind. Unreadable files are left alone (a
    writer may be mid-way, or newer than this ``tt``)."""
    outcome = GcOutcome()
    for holder in reading.expired:
        if _still_expired(holder, now):
            holder.path.unlink(missing_ok=True)
            outcome.claim_files.append(holder.path.name)
    revoked_dir = claims_dir / REVOKED_DIRNAME
    if revoked_dir.is_dir():
        for path in sorted(revoked_dir.glob("*.json")):
            # Orphaned: no claim file (any more) for the holder it is about.
            # An unreadable claim file counts as present.
            if claim_path(claims_dir, path.name.removesuffix(".json")).exists():
                continue
            path.unlink(missing_ok=True)
            outcome.revocation_files.append(f"{REVOKED_DIRNAME}/{path.name}")
    for directory in (claims_dir, revoked_dir):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(f".*{TEMP_SUFFIX}")):
            age = now - datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
            if age >= _STALE_TEMP_AGE:
                path.unlink(missing_ok=True)
                outcome.temp_files.append(str(path.relative_to(claims_dir)))
    return outcome


def _still_expired(holder: Holder, now: datetime) -> bool:
    """Re-reads the file: the holder may have written since the scan."""
    try:
        current = parse_claim_file(
            holder.path.read_text(), expected_holder=holder.holder
        )
    except (UnreadableFile, OSError):
        return False
    return classify(current, now) == "expired"
