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
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

lazy from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
)
lazy from claude_agent_sdk import query as sdk_query
lazy from claude_agent_sdk import tool as sdk_tool

from toolbelt.autonomous_agents.claim_writer import HEARTBEAT_SECONDS, RunClaim
from toolbelt.autonomous_agents.dispatcher import Decision
from toolbelt.autonomous_agents.instructions import (
    build_instructions,
    build_prompt,
    build_resume_prompt,
    report_summary,
)
from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.decisions.book import DecisionBook
from toolbelt.decisions.model import (
    Decision as AskedDecision,
)
from toolbelt.decisions.model import (
    InvalidDecision,
    describe_answers,
    points_from_data,
)
from toolbelt.git.stack.store import AgentRun, StackStore, TokenCounts
from toolbelt.logger import logger
from toolbelt.task_claims.resolution import TrackedTask
from toolbelt.token_spend.pricing import price_usage
from toolbelt.token_spend.transcripts import tokens_of

type Query = Callable[..., AsyncIterator[Any]]

#: What the agent may use. No subagents, no web: it works in the worktree
#: with git, gh and the repo's own tooling.
DECISIONS_SERVER = "toolbelt-decisions"
ALLOWED_TOOLS = [
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    f"mcp__{DECISIONS_SERVER}__propose_decisions",
    f"mcp__{DECISIONS_SERVER}__read_decisions",
]
DISALLOWED_TOOLS = ["Task", "WebFetch", "WebSearch", "NotebookEdit"]
#: Longest summary kept on a run.
SUMMARY_LIMIT = 4000
#: A working run's progress (turns, cost, what it is doing) is saved this often
#: at most, for the page to show.
PROGRESS_SECONDS = 3.0
#: Longest "what it is doing now" kept.
ACTIVITY_LIMIT = 160

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


def _first_line(text: str) -> str:
    return text.strip().split("\n", 1)[0].strip()


def _tool_activity(name: str, arguments: dict[str, Any]) -> str:
    """A tool call in a few words."""
    if name == "Bash":
        return f"Running: {_first_line(str(arguments.get('command', '')))}"
    if name in ("Read", "Edit", "Write"):
        verb = "Reading" if name == "Read" else "Editing"
        return f"{verb} {arguments.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"Searching for {arguments.get('pattern', '')}"
    return f"Using {name}"


def _activity(message: Any) -> str | None:
    """What an assistant message says the agent is doing: its last tool call,
    else the first line of its text; ``None`` when it says nothing."""
    tool: str | None = None
    text: str | None = None
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            tool = _tool_activity(block.name, block.input)
        elif isinstance(block, TextBlock) and block.text.strip():
            text = _first_line(block.text)
    found = tool or text
    return found[:ACTIVITY_LIMIT] if found else None


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
        clock: Callable[[], float] = time.monotonic,
        book: DecisionBook | None = None,
    ) -> None:
        self._db_path = db_path
        self._claims_dir = claims_dir
        self._limits = limits
        self._wall_clock = wall_clock
        self._query = query
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._book = book or DecisionBook(open_store=self._store, wall_clock=wall_clock)
        #: The runs working now, by run id.
        self.running: dict[str, asyncio.Task[None]] = {}
        #: Called when a run starts and when it ends.
        self.on_change: Callable[[], None] | None = None

    def _store(self) -> StackStore:
        return StackStore(self._db_path())

    def _decisions_server(
        self, decision: Decision, run_id: str, session_id: str
    ) -> Any:
        """The tools with which a run asks the user to decide. ``propose_decisions``
        records the question and tells the agent to stop: the run ends as
        ``deciding`` and is resumed with the answer (see ``resume``)."""
        from toolbelt.decisions.schema import proposal_schema

        book = self._book
        task = TrackedTask(
            repo_id=decision.repo_id,
            repo_name=decision.repo,
            branch=decision.branch,
            task_id=decision.task_id,
            task_ref=None,
            worktree=decision.worktree,
        )

        def text(message: str, *, error: bool = False) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": message}], "is_error": error}

        @sdk_tool(
            "propose_decisions",
            "Ask the user to decide. Records your decision points and the user "
            "answers later; then stop (end your turn with a one-line summary): "
            "you are resumed with the answers.",
            proposal_schema(),
        )
        async def propose_decisions(args: dict[str, Any]) -> dict[str, Any]:
            try:
                points = points_from_data(args.get("points"))
                await asyncio.to_thread(
                    lambda: book.propose(
                        task,
                        proposer="autonomous",
                        title=str(args.get("title", "")),
                        points=points,
                        session_id=session_id,
                        run_id=run_id,
                    )
                )
            except InvalidDecision as error:
                return text(str(error), error=True)
            return text(
                "Recorded. The user will answer in toolbelt. Stop now: end your turn "
                "with a one-line summary of where you stopped. You will be resumed "
                "with their answers."
            )

        @sdk_tool(
            "read_decisions",
            "The questions already asked about this task, and the user's answers.",
            {"type": "object", "properties": {}},
        )
        async def read_decisions(args: dict[str, Any]) -> dict[str, Any]:
            found = await asyncio.to_thread(book.for_task, decision.task_id)
            if not found:
                return text("No question was asked about this task.")
            return text(
                "\n\n".join(
                    describe_answers(d)
                    if d.status == "answered"
                    else f"Decision: {d.title}\n  (still waiting for the user)"
                    for d in found
                )
            )

        return create_sdk_mcp_server(
            DECISIONS_SERVER, tools=[propose_decisions, read_decisions]
        )

    def _options(
        self,
        decision: Decision,
        session_id: str,
        run_id: str = "",
        *,
        resuming: bool = False,
    ) -> Any:
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
            mcp_servers={
                DECISIONS_SERVER: self._decisions_server(decision, run_id, session_id)
            },
            # A resumed run continues the earlier run's session.
            resume=session_id if resuming else None,
            session_id=None if resuming else session_id,
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
        # The task is held from the moment its run is recorded, not once the
        # run's own task gets going.
        claim = RunClaim(self._claims_dir(), run_id, decision.worktree)
        claim.start(now)
        return self._launch(run_id, decision, session_id, claim, build_prompt(decision))

    def _launch(
        self,
        run_id: str,
        decision: Decision,
        session_id: str,
        claim: RunClaim,
        prompt: str,
        *,
        resuming: bool = False,
    ) -> str:
        task = asyncio.create_task(
            self._run(run_id, decision, session_id, claim, prompt, resuming)
        )
        self.running[run_id] = task
        task.add_done_callback(lambda _t: self.running.pop(run_id, None))
        if self.on_change is not None:
            self.on_change()
        return run_id

    async def resume(
        self, answered: AskedDecision, decision: Decision, prior: AgentRun
    ) -> str:
        """Continue a run that asked a question: a new run record (it is not
        another try at the job) resumes the earlier run's session with the
        answer as the next prompt. The earlier run is settled as finished."""
        worktree = decision.worktree
        if worktree is None or prior.session_id is None:
            raise ValueError("a run can only be resumed in its worktree and session")
        now = self._wall_clock()
        session_id = prior.session_id
        # The agent continues the job it asked about, not the one the branch
        # would get now.
        decision = replace(decision, trigger=prior.trigger)

        def create() -> str:
            store = self._store()
            try:
                run_id = store.start_agent_run(
                    task_id=decision.task_id,
                    repo_id=decision.repo_id,
                    branch=decision.branch,
                    trigger=prior.trigger,
                    head_sha=decision.head_sha,
                    model=self._limits.model,
                    at=now,
                    session_id=session_id,
                    resumed_from=prior.run_id,
                )
                store.settle_deciding_run(prior.run_id, state="succeeded")
                store.mark_decision_delivered(answered.id, now)
                return run_id
            finally:
                store.close()

        run_id = await asyncio.to_thread(create)
        claim = RunClaim(self._claims_dir(), run_id, worktree)
        claim.start(now)
        return self._launch(
            run_id,
            decision,
            session_id,
            claim,
            build_resume_prompt(describe_answers(answered)),
            resuming=True,
        )

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

    async def _run(
        self,
        run_id: str,
        decision: Decision,
        session_id: str,
        claim: RunClaim,
        prompt: str,
        resuming: bool,
    ) -> None:
        assert decision.worktree is not None
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        spend = _Spend()
        activity: str | None = None
        saved_at = self._clock()
        result: Any = None
        state = "failed"
        error: str | None = "the run ended without a result"
        try:
            query = self._query or sdk_query
            async with asyncio.timeout(self._limits.run_timeout_seconds):
                async for message in query(
                    prompt=prompt,
                    options=self._options(
                        decision, session_id, run_id, resuming=resuming
                    ),
                ):
                    if isinstance(message, AssistantMessage):
                        spend.add(message)
                        activity = _activity(message) or activity
                        if self._clock() - saved_at >= PROGRESS_SECONDS:
                            saved_at = self._clock()
                            await asyncio.to_thread(
                                self._save_progress, run_id, spend, activity
                            )
                            if self.on_change is not None:
                                self.on_change()
                    elif isinstance(message, ResultMessage):
                        result = message
            state, error = self._classify(result)
            if state == "succeeded" and await self._asked(run_id, decision.task_id):
                # It asked the user and stopped: it is resumed with the answer.
                state = "deciding"
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
            if self.on_change is not None:
                self.on_change()

    async def _asked(self, run_id: str, task_id: str) -> bool:
        """The run left a question for the user that is not answered yet."""
        found = await asyncio.to_thread(self._book.for_task, task_id)
        return any(d.run_id == run_id and d.status == "pending" for d in found)

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

    def _save_progress(self, run_id: str, spend: _Spend, activity: str | None) -> None:
        store = self._store()
        try:
            store.update_agent_run_progress(
                run_id,
                turns=spend.turns,
                cost_usd=spend.cost_usd,
                tokens=spend.tokens,
                activity=activity,
            )
        finally:
            store.close()

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
                error=None if state in ("succeeded", "deciding") else error,
            )
        finally:
            store.close()
