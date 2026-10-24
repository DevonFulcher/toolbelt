"""The activity chunk files the Claude Code plugin writes
(``$TOOLBELT_HOME/activity/<holder>.<turn>.<seq>.json``, contract in
``docs/agent-activity.md``): the tool calls a turn made, as the plugin timed
them. One file per chunk; the last chunk of a turn (``final``) carries the
turn's totals. Read and removed by ``ingest.py``."""

import json
from dataclasses import dataclass
from datetime import datetime

from toolbelt.git.stack.store import TurnTotals

ACTIVITY_DIRNAME = "activity"
VERSION = 1


class UnreadableChunk(Exception):
    """A chunk file that cannot be used, and why."""


@dataclass(frozen=True)
class CallRecord:
    tool: str
    detail: str
    started_at: datetime
    duration_ms: int
    blocked_ms: int
    ok: bool | None
    unended: bool
    background: bool
    delegated: bool
    worktree: str | None


@dataclass(frozen=True)
class Chunk:
    holder: str
    session_id: str
    agent_id: str | None
    turn_id: str
    seq: int
    final: bool
    started_at: datetime
    ended_at: datetime | None
    reason: str | None
    totals: TurnTotals | None
    calls: tuple[CallRecord, ...]

    @property
    def key(self) -> str:
        """Names the chunk, so reading it twice stores it once."""
        return f"{self.holder}/{self.turn_id}/{self.seq}"


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise UnreadableChunk(f"not a time: {value!r}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise UnreadableChunk(f"not a time: {value!r}") from error


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UnreadableChunk(f"not a number: {value!r}")
    return int(value)


def _str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value == "":
        raise UnreadableChunk(f"{key} is missing")
    return value


def _call(data: object) -> CallRecord:
    if not isinstance(data, dict):
        raise UnreadableChunk("a call is not an object")
    ok = data.get("ok")
    worktree = data.get("worktree")
    return CallRecord(
        tool=_str(data, "tool"),
        detail=str(data.get("detail", "")),
        started_at=_time(data.get("started_at")),
        duration_ms=_int(data.get("duration_ms")),
        blocked_ms=_int(data.get("blocked_ms", 0)),
        ok=ok if isinstance(ok, bool) else None,
        unended=bool(data.get("unended", False)),
        background=bool(data.get("background", False)),
        delegated=bool(data.get("delegated", False)),
        worktree=worktree if isinstance(worktree, str) else None,
    )


def parse_chunk(text: str) -> Chunk:
    """Parses a chunk file. Raises ``UnreadableChunk`` for anything a reader
    must skip: not JSON, another version, a missing field."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise UnreadableChunk(f"not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise UnreadableChunk("not a JSON object")
    if data.get("version") != VERSION:
        raise UnreadableChunk(f"unsupported version {data.get('version')!r}")
    totals = data.get("totals")
    ended = data.get("ended_at")
    agent = data.get("agent_id")
    reason = data.get("reason")
    calls = data.get("calls")
    if not isinstance(calls, list):
        raise UnreadableChunk("calls is missing")
    final = data.get("final") is True
    if final and not isinstance(totals, dict):
        raise UnreadableChunk("a final chunk has no totals")
    return Chunk(
        holder=_str(data, "holder"),
        session_id=_str(data, "session_id"),
        agent_id=agent if isinstance(agent, str) else None,
        turn_id=_str(data, "turn_id"),
        seq=_int(data.get("seq")),
        final=final,
        started_at=_time(data.get("started_at")),
        ended_at=_time(ended) if ended is not None else None,
        reason=reason if isinstance(reason, str) else None,
        totals=TurnTotals(
            wall_ms=_int(totals["wall_ms"]),
            tool_ms=_int(totals["tool_ms"]),
            delegated_ms=_int(totals["delegated_ms"]),
            blocked_ms=_int(totals["blocked_ms"]),
            model_ms=_int(totals["model_ms"]),
        )
        if isinstance(totals, dict)
        else None,
        calls=tuple(_call(call) for call in calls),
    )
