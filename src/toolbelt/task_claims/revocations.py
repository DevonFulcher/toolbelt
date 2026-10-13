"""Revocations: ``$TOOLBELT_HOME/claims/revoked/<holder>.json``, written only
by ``tt``.

``tt task release`` cannot edit an agent's own claim file (the agent rewrites
it every heartbeat), so it records what it took away here. The writer reads
its own revocation file before each write and drops the claims whose
``touched_at <= at``; a claim touched after the revocation stands."""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from toolbelt.task_claims.atomic_file import write_atomically
from toolbelt.task_claims.claim_file import (
    REVOKED_DIRNAME,
    SUPPORTED_VERSION,
    Claim,
    UnreadableFile,
    iso,
)


class WorktreeRevocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["worktree"]
    path: str
    at: AwareDatetime
    by: str


class RefRevocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["ref"]
    ref: str
    at: AwareDatetime
    by: str


type Revocation = WorktreeRevocation | RefRevocation


class RevocationFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: Literal[1]
    holder: str
    revoked: list[
        Annotated[WorktreeRevocation | RefRevocation, Field(discriminator="kind")]
    ]


def revocation_path(claims_dir: Path, holder: str) -> Path:
    return claims_dir / REVOKED_DIRNAME / f"{holder}.json"


def parse_revocations(text: str, *, expected_holder: str) -> RevocationFile:
    try:
        data = json.loads(text)
    except ValueError as error:
        raise UnreadableFile(f"not valid JSON: {error}") from error
    if not isinstance(data, dict) or data.get("version") != SUPPORTED_VERSION:
        raise UnreadableFile("not a version 1 revocation file")
    try:
        parsed = RevocationFile.model_validate(data)
    except ValueError as error:
        raise UnreadableFile(f"invalid: {error}") from error
    if parsed.holder != expected_holder:
        raise UnreadableFile(
            f"holder {parsed.holder!r} does not match the file name {expected_holder!r}"
        )
    return parsed


def read_revocations(claims_dir: Path, holder: str) -> RevocationFile | None:
    """``None`` when there is no file. Raises ``UnreadableFile`` for a broken one."""
    path = revocation_path(claims_dir, holder)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    return parse_revocations(text, expected_holder=holder)


def _key(item: Claim | Revocation) -> tuple[str, str]:
    return (item.kind, item.path if item.kind == "worktree" else item.ref)


def is_revoked(claim: Claim, revoked: list[Revocation]) -> bool:
    """A claim touched at or before a matching revocation is gone."""
    key = _key(claim)
    return any(_key(entry) == key and claim.touched_at <= entry.at for entry in revoked)


def record_revocations(
    claims_dir: Path, holder: str, claims: list[Claim], *, at: datetime, by: str
) -> None:
    """Revokes ``claims`` of ``holder`` as of ``at``, keeping earlier entries
    for other claims (an entry for the same claim is replaced)."""
    try:
        existing = read_revocations(claims_dir, holder)
    except UnreadableFile:
        existing = None  # tt owns this file: a broken one is rewritten
    kept = [] if existing is None else list(existing.revoked)
    new: list[Revocation] = []
    for claim in claims:
        if claim.kind == "worktree":
            new.append(
                WorktreeRevocation(kind="worktree", path=claim.path, at=at, by=by)
            )
        else:
            new.append(RefRevocation(kind="ref", ref=claim.ref, at=at, by=by))
    replaced = {_key(entry) for entry in new}
    merged = [entry for entry in kept if _key(entry) not in replaced] + new
    document = {
        "version": SUPPORTED_VERSION,
        "holder": holder,
        "revoked": [
            {
                "kind": entry.kind,
                **(
                    {"path": entry.path}
                    if entry.kind == "worktree"
                    else {"ref": entry.ref}
                ),
                "at": iso(entry.at),
                "by": entry.by,
            }
            for entry in merged
        ],
    }
    write_atomically(
        revocation_path(claims_dir, holder), json.dumps(document, indent=2) + "\n"
    )
