"""Reads the claims directory: every holder file, classified at a given
instant (the clock and the directory are arguments, so this is pure)."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from toolbelt.task_claims.claim_file import (
    REVOKED_DIRNAME,
    Claim,
    ClaimFile,
    UnreadableFile,
    parse_claim_file,
)
from toolbelt.task_claims.revocations import is_revoked, read_revocations

#: A ``working`` holder heartbeats every 30 s (its ttl is 180 s). One that has
#: not written for three heartbeats is "stale": still live by its ttl, but
#: probably hung.
STALE_AFTER = timedelta(seconds=90)

#: ``expired`` holders are ignored by readers (and deleted by ``tt task gc``).
type HolderStatus = Literal["working", "idle", "blocked", "expired"]


@dataclass(frozen=True)
class Holder:
    file: ClaimFile
    #: The path of the file this was read from.
    path: Path
    status: HolderStatus
    #: ``working`` and not heard from for ``STALE_AFTER``.
    stale: bool
    #: The claims that stand: ``file.claims`` less the revoked ones.
    claims: tuple[Claim, ...]
    revoked: tuple[Claim, ...]

    @property
    def live(self) -> bool:
        return self.status != "expired"

    @property
    def holder(self) -> str:
        return self.file.holder

    def ttl_left(self, now: datetime) -> timedelta:
        """Negative once expired."""
        return self.file.expires_at - now

    def age(self, now: datetime) -> timedelta:
        """How long since the holder last wrote its file."""
        return now - self.file.updated_at


@dataclass(frozen=True)
class SkippedFile:
    """A file readers ignore: unreadable, partial or of an unknown version."""

    name: str
    reason: str


@dataclass(frozen=True)
class Reading:
    holders: tuple[Holder, ...]
    skipped: tuple[SkippedFile, ...]

    @property
    def live(self) -> tuple[Holder, ...]:
        return tuple(holder for holder in self.holders if holder.live)

    @property
    def expired(self) -> tuple[Holder, ...]:
        return tuple(holder for holder in self.holders if not holder.live)


def classify(file: ClaimFile, now: datetime) -> HolderStatus:
    """Live iff ``now < updated_at + ttl_seconds``."""
    if now >= file.expires_at:
        return "expired"
    return file.state


def reclassify(holder: Holder, now: datetime) -> Holder:
    """``holder`` as it stands at ``now`` (a later instant than it was read)."""
    status = classify(holder.file, now)
    return replace(
        holder,
        status=status,
        stale=status == "working" and now - holder.file.updated_at >= STALE_AFTER,
    )


def read_claims_settled(
    claims_dir: Path, now: datetime, pause: Callable[[], None]
) -> Reading:
    """``read_claims``, listing a file as skipped only if it is unreadable on
    two reads ``pause`` apart: the writer overwrites in place (not atomically),
    so a single bad read is usually a write in progress."""
    first = read_claims(claims_dir, now)
    if not first.skipped:
        return first
    pause()
    second = read_claims(claims_dir, now)
    persistent = {s.name for s in first.skipped}
    return Reading(
        second.holders,
        tuple(s for s in second.skipped if s.name in persistent),
    )


def read_claims(claims_dir: Path, now: datetime) -> Reading:
    """Every holder file in ``claims_dir`` at ``now`` (holders in name order).

    A file that cannot be read is skipped and reported, never an error: a
    writer may be mid-way, or newer than this reader."""
    holders: list[Holder] = []
    skipped: list[SkippedFile] = []
    if not claims_dir.is_dir():
        return Reading((), ())
    for path in sorted(claims_dir.iterdir()):
        if path.suffix != ".json" or path.name.startswith(".") or not path.is_file():
            continue
        holder_id = path.name.removesuffix(".json")
        try:
            file = parse_claim_file(path.read_text(), expected_holder=holder_id)
        except (UnreadableFile, OSError) as error:
            skipped.append(SkippedFile(path.name, str(error)))
            continue
        try:
            revocations = read_revocations(claims_dir, holder_id)
        except (UnreadableFile, OSError) as error:
            # Its claims stand, as if nothing had been revoked.
            skipped.append(SkippedFile(f"{REVOKED_DIRNAME}/{path.name}", str(error)))
            revocations = None
        status = classify(file, now)
        revoked_entries = [] if revocations is None else revocations.revoked
        revoked = tuple(c for c in file.claims if is_revoked(c, revoked_entries))
        standing = tuple(c for c in file.claims if c not in revoked)
        holders.append(
            Holder(
                file=file,
                path=path,
                status=status,
                stale=status == "working" and now - file.updated_at >= STALE_AFTER,
                claims=standing,
                revoked=revoked,
            )
        )
    return Reading(tuple(holders), tuple(skipped))
