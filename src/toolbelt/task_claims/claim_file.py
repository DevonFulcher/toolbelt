"""The claim file (``docs/task-claims.md``): one JSON file per holder in
``$TOOLBELT_HOME/claims/``. Contract v1 is what the Claude Code plugin
writes; v2 is the same file with the ``autonomous`` holder kind, which only
toolbelt's own agents write. Readers accept both."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

CLAIMS_DIRNAME = "claims"
REVOKED_DIRNAME = "revoked"
#: The version the plugin and ``tt task claim`` write.
SUPPORTED_VERSION = 1
#: The version of a file with ``kind: "autonomous"``.
AUTONOMOUS_VERSION = 2
READABLE_VERSIONS = (SUPPORTED_VERSION, AUTONOMOUS_VERSION)

#: Holder ids are file names, so only these characters are allowed.
HOLDER_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

type HolderKind = Literal["agent", "human", "autonomous"]
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


class ClaimActivity(BaseModel):
    """What the holder is running: the tool call in flight (``open``), else the
    last that ended. Optional and no part of liveness."""

    model_config = ConfigDict(frozen=True)

    tool: str
    detail: str
    since: AwareDatetime
    open: bool


class ClaimFile(BaseModel):
    """What a holder's file says. Unknown extra fields are ignored."""

    model_config = ConfigDict(frozen=True)

    version: Literal[1, 2]
    holder: str = Field(pattern=HOLDER_PATTERN.pattern)
    kind: HolderKind
    session_id: str | None = None
    agent_id: str | None = None
    link: str | None = None
    #: An autonomous holder's run (the id of its ``agent_runs`` row).
    run_id: str | None = None
    state: HolderState
    started_at: AwareDatetime
    updated_at: AwareDatetime
    #: 0 is how a writer ends a holder it cannot delete: dead at once.
    ttl_seconds: int = Field(ge=0)
    #: The writer's process on this machine (both fields or neither): the
    #: holder is dead at once when that process is gone, or its pid was
    #: reused (a different start time). Files without them (``human.*``,
    #: older plugins) are live by ``ttl_seconds`` alone.
    pid: int | None = Field(default=None, ge=1)
    process_started_at: AwareDatetime | None = None
    #: What it is running (written by plugins that record activity).
    activity: ClaimActivity | None = None
    claims: list[Annotated[WorktreeClaim | RefClaim, Field(discriminator="kind")]]

    @model_validator(mode="after")
    def _autonomous_is_v2(self) -> ClaimFile:
        if (self.kind == "autonomous") != (self.version == AUTONOMOUS_VERSION):
            raise ValueError('kind "autonomous" is exactly contract version 2')
        return self

    @property
    def process(self) -> tuple[int, datetime] | None:
        """The writer's pid and start time, when the file records both."""
        if self.pid is None or self.process_started_at is None:
            return None
        return (self.pid, self.process_started_at)

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
    if data.get("version") not in READABLE_VERSIONS:
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
