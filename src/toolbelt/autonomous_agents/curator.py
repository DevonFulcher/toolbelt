"""Runs the curator agent: one Claude Agent SDK ``query`` with no tools that
turns the user's decisions on proposals into the guidelines the proposer is
given. Its run is recorded like any agent's (trigger ``curate``, no task), so it
counts toward the spend limits and shows on the Agents page.

``query`` is injectable so tests never start a model.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

lazy from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage
lazy from claude_agent_sdk import query as sdk_query

from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.autonomous_agents.proposer import DISALLOWED_TOOLS, PROPOSER_TASK
from toolbelt.autonomous_agents.runner import (
    SUMMARY_LIMIT,
    AgentRunner,
    Query,
    _is_auth_failure,
    _Spend,
)
from toolbelt.git.stack.store import StackStore
from toolbelt.logger import logger
from toolbelt.proposals.book import ProposalBook
from toolbelt.proposals.curation import build_curator_instructions
from toolbelt.token_spend.transcripts import tokens_of

CURATE = "curate"


class CuratorRunner:
    def __init__(
        self,
        *,
        db_path: Callable[[], Path],
        workdir: Callable[[], Path],
        limits: AgentLimits,
        book: ProposalBook,
        wall_clock: Callable[[], datetime],
        query: Query | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db_path = db_path
        self._workdir = workdir
        self._limits = limits
        self._book = book
        self._wall_clock = wall_clock
        self._query = query
        self._clock = clock
        self.running: asyncio.Task[None] | None = None
        self.on_change: Callable[[], None] | None = None

    def _store(self) -> StackStore:
        return StackStore(self._db_path())

    async def start(self) -> str:
        now = self._wall_clock()

        def create() -> str:
            store = self._store()
            try:
                return store.start_agent_run(
                    task_id=PROPOSER_TASK,
                    repo_id=0,
                    branch="",
                    trigger=CURATE,
                    head_sha="",
                    model=self._limits.propose_model,
                    at=now,
                )
            finally:
                store.close()

        run_id = await asyncio.to_thread(create)
        task = asyncio.create_task(self._run(run_id))
        self.running = task
        task.add_done_callback(lambda _t: setattr(self, "running", None))
        if self.on_change is not None:
            self.on_change()
        return run_id

    async def _run(self, run_id: str) -> None:
        spend = _Spend()
        result: Any = None
        state = "failed"
        error: str | None = "the run ended without a result"
        written = False
        try:
            decided = await asyncio.to_thread(
                lambda: [p for p in self._book.all() if p.status != "pending"]
            )
            previous = await asyncio.to_thread(self._book.guidelines)
            workdir = self._workdir()
            workdir.mkdir(parents=True, exist_ok=True)
            options = ClaudeAgentOptions(
                model=self._limits.propose_model,
                cwd=str(workdir),
                max_turns=3,
                max_budget_usd=self._limits.propose_run_usd,
                disallowed_tools=DISALLOWED_TOOLS,
                setting_sources=[],
                system_prompt=build_curator_instructions(decided, previous),
            )
            async with asyncio.timeout(self._limits.propose_timeout_seconds):
                async for message in (self._query or sdk_query)(
                    prompt="Write the guidelines now.", options=options
                ):
                    if isinstance(message, AssistantMessage):
                        spend.add(message)
                    elif isinstance(message, ResultMessage):
                        result = message
            state, error = AgentRunner._classify(result)
            if state == "succeeded" and (result.result or "").strip():
                await asyncio.to_thread(
                    lambda: self._book.write_guidelines(result.result, author="curator")
                )
                written = True
        except TimeoutError:
            state, error = "timed_out", "ran too long"
        except asyncio.CancelledError:
            state, error = "stopped", "stopped by request"
        except Exception as failure:  # noqa: BLE001 - recorded on the run
            logger.exception("a curator run failed")
            text = f"{type(failure).__name__}: {failure}"
            state = "failed"
            error = (
                "Claude is not signed in: run `claude auth login`. " + text
                if _is_auth_failure(text)
                else text
            )
        finally:
            await asyncio.shield(
                asyncio.to_thread(
                    self._finish, run_id, state, error, result, spend, written
                )
            )
            if self.on_change is not None:
                self.on_change()

    def _finish(
        self,
        run_id: str,
        state: str,
        error: str | None,
        result: Any,
        spend: _Spend,
        written: bool,
    ) -> None:
        store = self._store()
        try:
            store.finish_agent_run(
                run_id,
                state=state,
                ended_at=self._wall_clock(),
                session_id=result.session_id if result else None,
                turns=result.num_turns if result else spend.turns,
                cost_usd=result.total_cost_usd
                if result and result.total_cost_usd is not None
                else spend.cost_usd,
                tokens=tokens_of(result.usage)
                if result and result.usage
                else spend.tokens,
                summary=("Wrote new guidelines"[:SUMMARY_LIMIT] if written else None),
                error=None if state == "succeeded" else error,
            )
        finally:
            store.close()
