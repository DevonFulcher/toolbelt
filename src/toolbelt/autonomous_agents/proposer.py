"""Runs the proposer agent: one Claude Agent SDK ``query`` that reads the
user's connected MCP systems and writes proposals.

Unlike a run on a PR (``runner.AgentRunner``) it has no branch, no worktree and
no claim: its run record has the fixed task ``proposals`` and no repo. It loads
the user's own settings, so it sees the MCP servers (and skills) installed on
this machine, whichever they are; it may not run commands or edit files, and
the instructions tell it to only read. Its answer is structured output, which
is validated and stored through the ``ProposalBook``, whose cap decides how
many are kept.

``query`` is injectable so tests (and a dry run) never start a model.
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
from toolbelt.autonomous_agents.runner import (
    PROGRESS_SECONDS,
    SUMMARY_LIMIT,
    AgentRunner,
    Query,
    _activity,
    _is_auth_failure,
    _Spend,
)
from toolbelt.git.stack.store import StackStore
from toolbelt.logger import logger
from toolbelt.proposals.book import ProposalBook
from toolbelt.proposals.instructions import (
    PROMPT_VERSION,
    PROPOSAL_SCHEMA,
    build_instructions,
)
from toolbelt.proposals.model import InvalidProposal, Proposal, content_from_data
from toolbelt.token_spend.transcripts import tokens_of

#: The task id of every proposer run: they belong to no branch.
PROPOSER_TASK = "proposals"
PROPOSE = "propose"

#: Tools a proposer never gets: it reads, it does not change anything here.
DISALLOWED_TOOLS = [
    "Bash",
    "Edit",
    "Write",
    "NotebookEdit",
    "Task",
    "WebFetch",
    "WebSearch",
]


def proposals_from_output(output: object) -> tuple[list[tuple], list[str]]:
    """The proposals and the systems read in the proposer's structured output.
    A proposal that does not fit the shape is dropped (and logged): one bad
    item does not lose the rest."""
    if not isinstance(output, dict):
        return [], []
    items = []
    for raw in output.get("proposals") or []:
        try:
            items.append(
                (
                    str(raw["source"]).strip(),
                    str(raw["source_ref"]).strip(),
                    str(raw.get("source_url", "")).strip(),
                    content_from_data(raw),
                )
            )
        except (KeyError, InvalidProposal, AttributeError) as error:
            logger.warning("a proposal was dropped: %r", error)
    sources = [str(s) for s in output.get("sources_read") or []]
    return items, sources


class ProposerRunner:
    def __init__(
        self,
        *,
        db_path: Callable[[], Path],
        workdir: Callable[[], Path],
        limits: AgentLimits,
        book: ProposalBook,
        repos: Callable[[], list[str]],
        history: Callable[[], list[Proposal]],
        guidelines: Callable[[], str] = lambda: "",
        wall_clock: Callable[[], datetime],
        query: Query | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db_path = db_path
        self._workdir = workdir
        self._limits = limits
        self._book = book
        self._repos = repos
        self._history = history
        self._guidelines = guidelines
        self._wall_clock = wall_clock
        self._query = query
        self._clock = clock
        #: The run working now, if any.
        self.running: asyncio.Task[None] | None = None
        #: Called when a run starts, makes progress and ends.
        self.on_change: Callable[[], None] | None = None

    def _store(self) -> StackStore:
        return StackStore(self._db_path())

    def _options(self, slots: int) -> Any:
        workdir = self._workdir()
        workdir.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            model=self._limits.propose_model,
            cwd=str(workdir),
            max_turns=self._limits.propose_run_turns,
            max_budget_usd=self._limits.propose_run_usd,
            disallowed_tools=DISALLOWED_TOOLS,
            # The MCP tools are whatever this machine has installed, so they
            # cannot be listed: everything not disallowed is allowed, and the
            # instructions keep the run to reading.
            permission_mode="bypassPermissions",
            # The user's settings: their MCP servers, skills and plugins.
            setting_sources=["user"],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": build_instructions(
                    slots=slots,
                    repos=self._repos(),
                    history=self._history(),
                    guidelines=self._guidelines(),
                ),
            },
            output_format={"type": "json_schema", "schema": PROPOSAL_SCHEMA},
        )

    async def start(self, slots: int) -> str:
        """Record a run asked for ``slots`` proposals and start it in the
        background. Returns its id."""
        now = self._wall_clock()

        def create() -> str:
            store = self._store()
            try:
                return store.start_agent_run(
                    task_id=PROPOSER_TASK,
                    repo_id=0,
                    branch="",
                    trigger=PROPOSE,
                    head_sha="",
                    model=self._limits.propose_model,
                    at=now,
                    slots=slots,
                )
            finally:
                store.close()

        run_id = await asyncio.to_thread(create)
        task = asyncio.create_task(self._run(run_id, slots))
        self.running = task
        task.add_done_callback(lambda _t: setattr(self, "running", None))
        if self.on_change is not None:
            self.on_change()
        return run_id

    def stop(self) -> bool:
        if self.running is None:
            return False
        self.running.cancel()
        return True

    async def _run(self, run_id: str, slots: int) -> None:
        spend = _Spend()
        activity: str | None = None
        saved_at = self._clock()
        result: Any = None
        state = "failed"
        error: str | None = "the run ended without a result"
        stored: list[Proposal] = []
        try:
            options = await asyncio.to_thread(self._options, slots)
            query = self._query or sdk_query
            async with asyncio.timeout(self._limits.propose_timeout_seconds):
                async for message in query(
                    prompt=f"Find up to {slots} proposals now.", options=options
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
            state, error = AgentRunner._classify(result)
            if state == "succeeded":
                items, sources = proposals_from_output(result.structured_output)
                stored = await asyncio.to_thread(
                    lambda: self._book.propose(
                        items,
                        run_id=run_id,
                        sources_read=sources,
                        model=self._limits.propose_model,
                        prompt_version=PROMPT_VERSION,
                    )
                )
        except TimeoutError:
            state = "timed_out"
            error = (
                f"ran longer than {self._limits.propose_timeout_seconds // 60} minutes"
            )
        except asyncio.CancelledError:
            state, error = "stopped", "stopped by request"
        except Exception as failure:  # noqa: BLE001 - recorded on the run
            logger.exception("a proposer run failed")
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
                    self._finish, run_id, state, error, result, spend, stored, slots
                )
            )
            if self.on_change is not None:
                self.on_change()

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
        stored: list[Proposal],
        slots: int,
    ) -> None:
        if state == "succeeded":
            titles = "; ".join(p.content.title for p in stored)
            summary = f"Proposed {len(stored)} of {slots}" + (
                f": {titles}" if titles else ""
            )
        else:
            summary = None
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
                summary=summary[:SUMMARY_LIMIT] if summary else None,
                error=None if state == "succeeded" else error,
            )
        finally:
            store.close()
