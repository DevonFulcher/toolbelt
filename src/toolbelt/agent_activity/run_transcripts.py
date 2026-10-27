"""The tool calls of an autonomous run, from its Claude Code transcript.

The plugin records an interactive session's calls as it goes (``chunks.py``),
but a run loads only the project's settings, not the user's plugins, so nothing
records it. When a run has ended, its transcript says when each tool call began
(the assistant's ``tool_use``) and ended (the ``tool_result``): they are turned
into one final chunk per run, stored like the plugin's (``ingest.py``) and
shown with them in the aggregate view. A run is one turn, named
``run-<run id>``."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from toolbelt.agent_activity.chunks import CallRecord, Chunk
from toolbelt.agent_activity.ingest import RETAIN_CALLS, ActivityIngest
from toolbelt.git.stack.store import AgentRun, StackStore, TurnTotals, default_db_path
from toolbelt.logger import logger

TURN_PREFIX = "run-"
#: Run entries are read from this much before the run started to this much
#: after it ended: a resumed run's session holds the runs before it too.
_WINDOW_SLACK = timedelta(seconds=5)
#: What a call is shown by, the first the tool's input has (as the plugin
#: records it), cut to ``_DETAIL_LIMIT`` characters.
_DETAIL_KEYS = (
    "command",
    "file_path",
    "path",
    "pattern",
    "url",
    "query",
    "text",
    "description",
    "prompt",
)
_DETAIL_LIMIT = 500
#: Tools that start a subagent: the call's time is spent waiting on it.
_SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
DEFAULT_INTERVAL_SECONDS = 60.0


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _detail(tool_input: dict) -> str:
    for key in _DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value[:_DETAIL_LIMIT]
    return ""


def _union_ms(intervals: list[tuple[datetime, datetime]]) -> int:
    """The time the intervals cover, parallel ones counted once."""
    covered = timedelta()
    end: datetime | None = None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            covered += stop - start
            end = stop
        elif stop > end:
            covered += stop - end
            end = stop
    return int(covered.total_seconds() * 1000)


def run_chunk(run: AgentRun, transcript: Path) -> Chunk:
    """The final chunk of ``run``, from the entries of ``transcript`` made
    while it ran."""
    assert run.ended_at is not None and run.session_id is not None
    first, last = run.started_at - _WINDOW_SLACK, run.ended_at + _WINDOW_SLACK
    open_calls: dict[str, tuple[datetime, str, dict, str | None]] = {}
    calls: list[CallRecord] = []
    worktree: str | None = None

    def record(
        started: datetime,
        stopped: datetime,
        tool: str,
        tool_input: dict,
        where: str | None,
        *,
        ok: bool | None,
        unended: bool,
    ) -> None:
        calls.append(
            CallRecord(
                tool=tool,
                detail=_detail(tool_input),
                started_at=started,
                duration_ms=max(int((stopped - started).total_seconds() * 1000), 0),
                blocked_ms=0,
                ok=ok,
                unended=unended,
                background=tool_input.get("run_in_background") is True,
                delegated=tool in _SUBAGENT_TOOLS,
                worktree=where,
            )
        )

    with transcript.open() as lines:
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            stamp = entry.get("timestamp")
            if not isinstance(stamp, str):
                continue
            at = _time(stamp)
            if not first <= at <= last:
                continue
            worktree = entry.get("cwd") or worktree
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    open_calls[block["id"]] = (
                        at,
                        block["name"],
                        block.get("input") or {},
                        worktree,
                    )
                elif (
                    block.get("type") == "tool_result"
                    and block.get("tool_use_id") in open_calls
                ):
                    started, tool, tool_input, where = open_calls.pop(
                        block["tool_use_id"]
                    )
                    record(
                        started,
                        at,
                        tool,
                        tool_input,
                        where,
                        ok=not block.get("is_error", False),
                        unended=False,
                    )
    for started, tool, tool_input, where in open_calls.values():
        record(started, run.ended_at, tool, tool_input, where, ok=None, unended=True)
    calls.sort(key=lambda call: call.started_at)

    def span(call: CallRecord) -> tuple[datetime, datetime]:
        return call.started_at, call.started_at + timedelta(
            milliseconds=call.duration_ms
        )

    wall_ms = int((run.ended_at - run.started_at).total_seconds() * 1000)
    tool_ms = _union_ms([span(call) for call in calls if not call.delegated])
    delegated_ms = _union_ms([span(call) for call in calls if call.delegated])
    return Chunk(
        holder=run.session_id,
        session_id=run.session_id,
        agent_id=None,
        turn_id=f"{TURN_PREFIX}{run.run_id}",
        seq=0,
        final=True,
        started_at=run.started_at,
        ended_at=run.ended_at,
        reason=run.state,
        totals=TurnTotals(
            wall_ms=wall_ms,
            tool_ms=tool_ms,
            delegated_ms=delegated_ms,
            blocked_ms=0,
            model_ms=max(wall_ms - tool_ms - delegated_ms, 0),
        ),
        calls=tuple(calls),
    )


class RunTranscripts:
    """Stores the calls of every run that has ended and is not stored yet: a run
    just ended, and the runs from before this recording existed (back to
    ``RETAIN_CALLS``)."""

    def __init__(
        self,
        *,
        ingest: ActivityIngest,
        projects: Callable[[], Path],
        db_path: Callable[[], Path] = default_db_path,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        interval: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._ingest = ingest
        self._projects = projects
        self._db_path = db_path
        self._wall_clock = wall_clock
        self._interval = interval
        #: Runs whose transcript was not found: not looked for again until
        #: ``tt ui`` restarts.
        self._missing: set[str] = set()

    def poll(self) -> int:
        """Stores the runs there are to store. Returns how many runs."""
        store = StackStore(self._db_path())
        try:
            runs = store.runs_without_activity(
                since=self._wall_clock() - RETAIN_CALLS, turn_prefix=TURN_PREFIX
            )
        finally:
            store.close()
        chunks = []
        for run in runs:
            if run.run_id in self._missing:
                continue
            assert run.session_id is not None
            found = list(self._projects().glob(f"*/{run.session_id}.jsonl"))
            if not found:
                self._missing.add(run.run_id)
                continue
            chunks.append(run_chunk(run, found[0]))
        return self._ingest.ingest_chunks(chunks) if chunks else 0

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.poll)
            except Exception:  # noqa: BLE001
                # A background reader: one bad pass must not stop the next.
                logger.exception("reading the run transcripts failed")
            await asyncio.sleep(self._interval)
