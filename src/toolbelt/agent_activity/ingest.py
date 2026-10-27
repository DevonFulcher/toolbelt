"""Brings the plugin's activity chunk files into the stack store, and removes
them: each call classified (``shell.py``) and tied to the task whose worktree
it was about, a turn's row made when its final chunk arrives. Housekeeping on
the way: turns whose writer died are closed, and raw calls older than
``RETAIN_CALLS`` are dropped (the turns' rows are kept)."""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from toolbelt.agent_activity.chunks import Chunk, UnreadableChunk, parse_chunk
from toolbelt.agent_activity.shell import classify
from toolbelt.git.stack.store import StackStore, ToolCallRow, default_db_path
from toolbelt.logger import logger
from toolbelt.task_claims.claim_file import WorktreeClaim
from toolbelt.task_claims.resolution import TrackedTask, resolve_claim

#: How long the raw calls are kept; the per-turn rows are kept for good.
RETAIN_CALLS = timedelta(days=60)
#: A turn with calls and no final chunk for this long lost its writer.
CLOSE_UNFINISHED_AFTER = timedelta(hours=1)
#: An unreadable file this old is not one that is still being written.
UNREADABLE_AFTER_SECONDS = 600.0
DEFAULT_INTERVAL_SECONDS = 5.0
_HOUSEKEEPING_EVERY = timedelta(minutes=10)


class ActivityIngest:
    def __init__(
        self,
        *,
        directory: Callable[[], Path],
        tasks: Callable[[], list[TrackedTask]],
        db_path: Callable[[], Path] = default_db_path,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        interval: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._directory = directory
        self._tasks = tasks
        self._db_path = db_path
        self._wall_clock = wall_clock
        self._interval = interval
        self._housekept: datetime | None = None

    def poll(self) -> int:
        """Reads every chunk file there is. Returns how many were new."""
        directory = self._directory()
        directory.mkdir(parents=True, exist_ok=True)
        files = sorted(directory.glob("*.json"))
        now = self._wall_clock()
        due = self._housekept is None or now - self._housekept >= _HOUSEKEEPING_EVERY
        if not files and not due:
            return 0
        store = StackStore(self._db_path())
        try:
            stored = self._read(store, files, now) if files else 0
            if due:
                self._housekeep(store, now)
            return stored
        finally:
            store.close()

    def ingest_chunks(self, chunks: list[Chunk]) -> int:
        """Stores chunks that did not come from files (a run's transcript).
        Returns how many were new."""
        store = StackStore(self._db_path())
        try:
            tasks = self._tasks()
            now = self._wall_clock()
            return sum(self._store(store, chunk, tasks, {}, now) for chunk in chunks)
        finally:
            store.close()

    def _read(self, store: StackStore, files: list[Path], now: datetime) -> int:
        tasks = self._tasks()
        resolved: dict[str, tuple[str, int] | None] = {}
        stored = 0
        chunks: list[tuple[Chunk, Path]] = []
        for path in files:
            try:
                chunk = parse_chunk(path.read_text())
            except OSError:
                continue
            except UnreadableChunk as problem:
                # The writer overwrites in place: a half-written file reads again
                # next time; one that stays unreadable is dropped.
                if time.time() - path.stat().st_mtime > UNREADABLE_AFTER_SECONDS:
                    logger.warning("dropping activity file %s: %s", path.name, problem)
                    path.unlink(missing_ok=True)
                continue
            chunks.append((chunk, path))
        # A turn's chunks in order, so its row is made from all its calls.
        for chunk, path in sorted(
            chunks, key=lambda item: (item[0].holder, item[0].turn_id, item[0].seq)
        ):
            if self._store(store, chunk, tasks, resolved, now):
                stored += 1
            path.unlink(missing_ok=True)
        return stored

    def _store(
        self,
        store: StackStore,
        chunk: Chunk,
        tasks: list[TrackedTask],
        resolved: dict[str, tuple[str, int] | None],
        now: datetime,
    ) -> bool:
        def owner(worktree: str | None) -> tuple[str, int] | None:
            if worktree is None:
                return None
            if worktree not in resolved:
                matched = resolve_claim(
                    WorktreeClaim(kind="worktree", path=worktree, touched_at=now), tasks
                )
                resolved[worktree] = (
                    (matched[0].task_id, matched[0].repo_id) if matched else None
                )
            return resolved[worktree]

        rows = []
        for call in chunk.calls:
            command = classify(call.tool, call.detail)
            task = owner(call.worktree)
            rows.append(
                ToolCallRow(
                    holder=chunk.holder,
                    turn_id=chunk.turn_id,
                    task_id=task[0] if task else None,
                    repo_id=task[1] if task else None,
                    tool=call.tool,
                    detail=call.detail,
                    program=command.program,
                    subcommand=command.subcommand,
                    category=command.category,
                    started_at=call.started_at,
                    duration_ms=call.duration_ms,
                    blocked_ms=call.blocked_ms,
                    ok=call.ok,
                    unended=call.unended,
                    background=call.background,
                    delegated=call.delegated,
                    worktree=call.worktree,
                )
            )
        turn = None
        if chunk.final and chunk.totals is not None and chunk.ended_at is not None:
            turn = (
                chunk.holder,
                chunk.turn_id,
                chunk.session_id,
                chunk.agent_id,
                chunk.started_at,
                chunk.ended_at,
                chunk.reason,
                chunk.totals,
            )
        return store.ingest_activity_chunk(chunk.key, rows, turn=turn, at=now)

    def _housekeep(self, store: StackStore, now: datetime) -> None:
        store.close_unfinished_turns(idle_for=CLOSE_UNFINISHED_AFTER, now=now)
        store.prune_tool_calls(now - RETAIN_CALLS)
        self._housekept = now

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.poll)
            except Exception:  # noqa: BLE001
                # A background reader: one bad pass must not stop the next.
                logger.exception("reading the activity files failed")
            await asyncio.sleep(self._interval)
