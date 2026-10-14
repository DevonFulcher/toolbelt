"""Runs one autonomous agent on one task, with the Claude Agent SDK.

A run is: a run record (``agent_runs``), a claim file so everything else sees
the task is held, one SDK ``query`` in the task's worktree under the run's
dollar, turn and time limits, and then the record finished with what the SDK
reports (cost, turns, the agent's own summary) and how it ended. The SDK uses
Claude Code's own login, so a run draws on the same account limits as an
interactive session.

``query`` is injectable so tests (and a dry run) never start a model.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

lazy from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage
lazy from claude_agent_sdk import query as sdk_query

from toolbelt.autonomous_agents.claim_writer import HEARTBEAT_SECONDS, RunClaim
from toolbelt.autonomous_agents.dispatcher import Decision
from toolbelt.autonomous_agents.instructions import (
    build_instructions,
    build_prompt,
    report_summary,
)
from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.git.stack.store import StackStore, TokenCounts
from toolbelt.logger import logger
from toolbelt.token_spend.pricing import price_usage
from toolbelt.token_spend.transcripts import tokens_of

type Query = Callable[..., AsyncIterator[Any]]

#: What the agent may use. No subagents, no web: it works in the worktree
#: with git, gh and the repo's own tooling.
ALLOWED_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]
DISALLOWED_TOOLS = ["Task", "WebFetch", "WebSearch", "NotebookEdit"]
#: Longest summary kept on a run.
SUMMARY_LIMIT = 4000

_AUTH_HINTS = ("oauth", "authenticate", "not logged in", "please run /login")


@dataclass
class _Spend:
    """What a run has spent so far, from its assistant messages (a response
    arrives on several messages with the same id; the largest is kept). It is
    what is recorded when a run ends without the SDK's own totals."""

    seen: dict[str, tuple[int, float, TokenCounts]] = field(default_factory=dict)

    def add(self, message: Any) -> None:
        usage = message.usage or {}
        tokens = tokens_of(usage)
        cost = price_usage(message.model, usage) or 0.0
        key = message.message_id or f"anon-{len(self.seen)}"
        previous = self.seen.get(key)
        if previous is None or tokens.total >= previous[0]:
            self.seen[key] = (tokens.total, cost, tokens)

    @property
    def cost_usd(self) -> float:
        return sum(cost for _, cost, _ in self.seen.values())

    @property
    def turns(self) -> int:
        return len(self.seen)

    @property
    def tokens(self) -> TokenCounts:
        parts = [tokens for _, _, tokens in self.seen.values()]
        return TokenCounts(
            input=sum(t.input for t in parts),
            output=sum(t.output for t in parts),
            cache_read=sum(t.cache_read for t in parts),
            cache_write_5m=sum(t.cache_write_5m for t in parts),
            cache_write_1h=sum(t.cache_write_1h for t in parts),
        )


def _is_auth_failure(text: str) -> bool:
    folded = text.casefold()
    return any(hint in folded for hint in _AUTH_HINTS)


class AgentRunner:
    def __init__(
        self,
        *,
        db_path: Callable[[], Path],
        claims_dir: Callable[[], Path],
        limits: AgentLimits,
        wall_clock: Callable[[], datetime],
        query: Query | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._db_path = db_path
        self._claims_dir = claims_dir
        self._limits = limits
        self._wall_clock = wall_clock
        self._query = query
        self._heartbeat_seconds = heartbeat_seconds
        #: The runs working now, by run id.
        self.running: dict[str, asyncio.Task[None]] = {}

    def _store(self) -> StackStore:
        return StackStore(self._db_path())

    def _options(self, decision: Decision, session_id: str) -> Any:
        return ClaudeAgentOptions(
            model=self._limits.model,
            cwd=str(decision.worktree),
            max_turns=self._limits.run_turns,
            max_budget_usd=self._limits.run_usd,
            allowed_tools=ALLOWED_TOOLS,
            disallowed_tools=DISALLOWED_TOOLS,
            permission_mode="acceptEdits",
            # The repo's own CLAUDE.md applies, not this machine's plugins,
            # skills or MCP servers.
            setting_sources=["project"],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": build_instructions(decision, self._limits),
            },
            session_id=session_id,
        )

    async def start(self, decision: Decision) -> str:
        """Record a run for ``decision`` and start it in the background.
        Returns its id."""
        if decision.worktree is None:
            raise ValueError("a run needs the branch's worktree")
        now = self._wall_clock()
        session_id = str(uuid.uuid4())

        def create() -> str:
            store = self._store()
            try:
                return store.start_agent_run(
                    task_id=decision.task_id,
                    repo_id=decision.repo_id,
                    branch=decision.branch,
                    trigger=decision.trigger,
                    head_sha=decision.head_sha,
                    model=self._limits.model,
                    at=now,
                    session_id=session_id,
                )
            finally:
                store.close()

        run_id = await asyncio.to_thread(create)
        task = asyncio.create_task(self._run(run_id, decision, session_id))
        self.running[run_id] = task
        task.add_done_callback(lambda _t: self.running.pop(run_id, None))
        return run_id

    def stop(self, run_id: str) -> bool:
        """Stop a run that is working; False if there is no such run."""
        task = self.running.get(run_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def _heartbeat(self, claim: RunClaim) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            claim.heartbeat(self._wall_clock())

    async def _run(self, run_id: str, decision: Decision, session_id: str) -> None:
        assert decision.worktree is not None
        claim = RunClaim(self._claims_dir(), run_id, decision.worktree)
        claim.start(self._wall_clock())
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        spend = _Spend()
        result: Any = None
        state = "failed"
        error: str | None = "the run ended without a result"
        try:
            query = self._query or sdk_query
            async with asyncio.timeout(self._limits.run_timeout_seconds):
                async for message in query(
                    prompt=build_prompt(decision),
                    options=self._options(decision, session_id),
                ):
                    if isinstance(message, AssistantMessage):
                        spend.add(message)
                    elif isinstance(message, ResultMessage):
                        result = message
            state, error = self._classify(result)
        except TimeoutError:
            state = "timed_out"
            error = f"ran longer than {self._limits.run_timeout_seconds // 60} minutes"
        except asyncio.CancelledError:
            state, error = "stopped", "stopped by request"
        except Exception as failure:  # noqa: BLE001 - recorded on the run
            logger.exception("an autonomous run failed")
            text = f"{type(failure).__name__}: {failure}"
            state = "failed"
            error = (
                "Claude is not signed in: run `claude auth login`. " + text
                if _is_auth_failure(text)
                else text
            )
        finally:
            heartbeat.cancel()
            claim.end(self._wall_clock())
            self._finish(run_id, state, error, result, spend, session_id)

    @staticmethod
    def _classify(result: Any) -> tuple[str, str | None]:
        if result is None:
            return "failed", "the run ended without a result"
        if result.subtype in ("error_max_turns", "error_max_budget_usd"):
            return "cap_reached", result.subtype
        if result.is_error or result.subtype != "success":
            text = result.result or "; ".join(result.errors or []) or result.subtype
            if _is_auth_failure(text):
                return "failed", "Claude is not signed in: run `claude auth login`."
            return "failed", text
        return "succeeded", None

    def _finish(
        self,
        run_id: str,
        state: str,
        error: str | None,
        result: Any,
        spend: _Spend,
        session_id: str,
    ) -> None:
        store = self._store()
        try:
            store.finish_agent_run(
                run_id,
                state=state,
                ended_at=self._wall_clock(),
                session_id=(result.session_id if result else None) or session_id,
                turns=result.num_turns if result else spend.turns,
                cost_usd=result.total_cost_usd
                if result and result.total_cost_usd is not None
                else spend.cost_usd,
                tokens=tokens_of(result.usage)
                if result and result.usage
                else spend.tokens,
                summary=report_summary(result.result or "")[:SUMMARY_LIMIT]
                if result
                else None,
                error=None if state == "succeeded" else error,
            )
        finally:
            store.close()
