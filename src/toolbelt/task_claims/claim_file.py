"""The claim file of contract v1 (``docs/task-claims.md``): one JSON file per
holder in ``$TOOLBELT_HOME/claims/``."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

CLAIMS_DIRNAME = "claims"
REVOKED_DIRNAME = "revoked"
SUPPORTED_VERSION = 1

#: Holder ids are file names, so only these characters are allowed.
HOLDER_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

type HolderKind = Literal["agent", "human"]
type HolderState = Literal["working", "idle", "blocked"]


class WorktreeClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["worktree"]
    path: str
    touched_at: AwareDatetime


class RefClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["ref"]
    ref: str
    touched_at: AwareDatetime


type Claim = WorktreeClaim | RefClaim


class ClaimFile(BaseModel):
    """What a holder's file says. Unknown extra fields are ignored."""

    model_config = ConfigDict(frozen=True)

    version: Literal[1]
    holder: str = Field(pattern=HOLDER_PATTERN.pattern)
    kind: HolderKind
    session_id: str | None = None
    agent_id: str | None = None
    link: str | None = None
    state: HolderState
    started_at: AwareDatetime
    updated_at: AwareDatetime
    ttl_seconds: int = Field(gt=0)
    claims: list[Annotated[WorktreeClaim | RefClaim, Field(discriminator="kind")]]

    @property
    def expires_at(self) -> datetime:
        return self.updated_at + timedelta(seconds=self.ttl_seconds)


class UnreadableFile(Exception):
    """A claim (or revocation) file a reader must ignore, with why."""


def parse_claim_file(text: str, *, expected_holder: str) -> ClaimFile:
    """Parses ``text`` (the content of ``<expected_holder>.json``).

    Raises ``UnreadableFile`` for anything a reader must ignore: not JSON, an
    unknown version, a missing field, or a ``holder`` that is not the file's
    own name."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise UnreadableFile(f"not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise UnreadableFile("not a JSON object")
    if data.get("version") != SUPPORTED_VERSION:
        raise UnreadableFile(f"unsupported version {data.get('version')!r}")
    try:
        parsed = ClaimFile.model_validate(data)
    except ValueError as error:
        raise UnreadableFile(f"invalid: {error}") from error
    if parsed.holder != expected_holder:
        raise UnreadableFile(
            f"holder {parsed.holder!r} does not match the file name {expected_holder!r}"
        )
    return parsed


def claim_key(claim: Claim) -> tuple[str, str]:
    """What identifies a claim within a holder: its kind and path / ref."""
    return (claim.kind, claim.path if claim.kind == "worktree" else claim.ref)


def iso(time: datetime) -> str:
    """The contract's timestamp form: UTC with a ``Z``."""
    return time.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
