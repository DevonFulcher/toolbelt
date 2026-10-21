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
from toolbelt.task_claims.process_liveness import ProcessLiveness
from toolbelt.task_claims.revocations import is_revoked, read_revocations

#: A ``working`` holder heartbeats every 30 s (its ttl is 180 s). One that has
#: not written for three heartbeats is "stale": still live by its ttl, but
#: probably hung.
STALE_AFTER = timedelta(seconds=90)

#: The checker ``tt`` uses (and ``tt ui`` shares across its ticks, so its
#: start-time cache survives from one tick to the next).
SYSTEM_LIVENESS = ProcessLiveness()

#: ``expired`` holders are ignored by readers (and deleted by ``tt task gc``).
type HolderStatus = Literal["working", "idle", "blocked", "expired"]


@dataclass(frozen=True)
class Holder:
    file: ClaimFile
    #: The path of the file this was read from.
    path: Path
    status: HolderStatus
    #: The file records its writer's process and that process is gone (or its
    #: pid was reused): ``expired`` whatever the ttl says.
    process_gone: bool
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


def process_gone(file: ClaimFile, now: datetime, liveness: ProcessLiveness) -> bool:
    """The file records its writer's process and it is not running (or its
    pid now belongs to another process). A file without them, or one already
    past its ttl (nothing left to decide), is never "gone"."""
    process = file.process
    return (
        process is not None
        and now < file.expires_at
        and not liveness.is_alive(*process)
    )


def classify(
    file: ClaimFile, now: datetime, *, is_process_gone: bool = False
) -> HolderStatus:
    """Live iff ``now < updated_at + ttl_seconds`` and the writer's process,
    if the file records it, is still the one running (``is_process_gone``
    is that check's answer, see ``process_gone``)."""
    if is_process_gone or now >= file.expires_at:
        return "expired"
    return file.state


def reclassify(
    holder: Holder, now: datetime, liveness: ProcessLiveness = SYSTEM_LIVENESS
) -> Holder:
    """``holder`` as it stands at ``now`` (a later instant than it was read)."""
    gone = process_gone(holder.file, now, liveness)
    status = classify(holder.file, now, is_process_gone=gone)
    return replace(
        holder,
        status=status,
        process_gone=gone,
        stale=status == "working" and now - holder.file.updated_at >= STALE_AFTER,
    )


def read_claims_settled(
    claims_dir: Path,
    now: datetime,
    pause: Callable[[], None],
    liveness: ProcessLiveness = SYSTEM_LIVENESS,
) -> Reading:
    """``read_claims``, listing a file as skipped only if it is unreadable on
    two reads ``pause`` apart: the writer overwrites in place (not atomically),
    so a single bad read is usually a write in progress."""
    first = read_claims(claims_dir, now, liveness)
    if not first.skipped:
        return first
    pause()
    second = read_claims(claims_dir, now, liveness)
    persistent = {s.name for s in first.skipped}
    return Reading(
        second.holders,
        tuple(s for s in second.skipped if s.name in persistent),
    )


def read_claims(
    claims_dir: Path, now: datetime, liveness: ProcessLiveness = SYSTEM_LIVENESS
) -> Reading:
    """Every holder file in ``claims_dir`` at ``now`` (holders in name order).
    A holder whose file records its writer's process is also ``expired`` when
    that process is gone, by ``liveness`` (this machine only).

    A file that cannot be read is skipped and reported, never an error: a
    writer may be mid-way, or newer than this reader."""
    holders: list[Holder] = []
    skipped: list[SkippedFile] = []
    if not claims_dir.is_dir():
        return Reading((), ())
    parsed: list[tuple[Path, str, ClaimFile]] = []
    for path in sorted(claims_dir.iterdir()):
        if path.suffix != ".json" or path.name.startswith(".") or not path.is_file():
            continue
        holder_id = path.name.removesuffix(".json")
        try:
            file = parse_claim_file(path.read_text(), expected_holder=holder_id)
        except (UnreadableFile, OSError) as error:
            skipped.append(SkippedFile(path.name, str(error)))
            continue
        parsed.append((path, holder_id, file))
    # one ps call for every pid not already cached
    liveness.prime(
        file.pid
        for _, _, file in parsed
        if file.pid is not None and file.process is not None and now < file.expires_at
    )
    for path, holder_id, file in parsed:
        try:
            revocations = read_revocations(claims_dir, holder_id)
        except (UnreadableFile, OSError) as error:
            # Its claims stand, as if nothing had been revoked.
            skipped.append(SkippedFile(f"{REVOKED_DIRNAME}/{path.name}", str(error)))
            revocations = None
        gone = process_gone(file, now, liveness)
        status = classify(file, now, is_process_gone=gone)
        revoked_entries = [] if revocations is None else revocations.revoked
        revoked = tuple(c for c in file.claims if is_revoked(c, revoked_entries))
        standing = tuple(c for c in file.claims if c not in revoked)
        holders.append(
            Holder(
                file=file,
                path=path,
                status=status,
                process_gone=gone,
                stale=status == "working" and now - file.updated_at >= STALE_AFTER,
                claims=standing,
                revoked=revoked,
            )
        )
    return Reading(tuple(holders), tuple(skipped))
