"""The MCP server an interactive Claude session uses to ask the user to decide.

``propose_decisions`` records the question in toolbelt and waits, with no limit
of its own, for the answer: the user gives it in the MCP App this tool shows (see
``app.html``) or in ``tt ui``. The session reads the answer as the tool's result.
``read_decisions`` lets any session, including a later one, read the questions of
a task and their answers: a question can be answered after the session that asked
it has closed. ``pending_decision`` and ``answer_decision`` are for the App only.

The server is a stdio process of its own, so it reads and writes the stack
database like every other toolbelt process; ``tt ui`` notices the change.
"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from toolbelt.decisions.book import AlreadyAnswered, DecisionBook, NoSuchDecision
from toolbelt.decisions.model import (
    Decision,
    InvalidDecision,
    describe_answers,
    points_from_data,
)
from toolbelt.decisions.schema import AnswerInput, PointInput
from toolbelt.decisions.tasks import AmbiguousTask, NoTask, resolve_task
from toolbelt.git.stack.store import StackStore, default_db_path
from toolbelt.task_claims.resolution import TrackedTask, git_worktrees, tracked_tasks

APP_URI = "ui://toolbelt/decisions.html"
APP_HTML = Path(__file__).with_name("app.html")

#: How often the database is read while waiting for an answer.
POLL_SECONDS = 2.0
#: How many polls between progress notifications. Claude Code aborts a stdio
#: tool call that is silent for 30 minutes by default; progress keeps it alive.
PROGRESS_EVERY = 10

INSTRUCTIONS = """\
Use propose_decisions when a choice is the user's to make and you cannot settle it from the \
request, the code or a sensible default: give each decision point a context, options (each with \
what picking it means, and its context, benefits, costs and risks) and your recommendation. \
The call waits for the user, however long it takes, and returns their answers: do not continue \
past it, and do not ask the same question in chat. If it is still running when the session \
seems idle, that is expected. Use read_decisions to see the questions already asked about a \
task and any answers (a question may have been answered after its session closed)."""


def _load_tasks() -> list[TrackedTask]:
    store = StackStore(default_db_path())
    try:
        return tracked_tasks(store, git_worktrees)
    finally:
        store.close()


def _decision_json(decision: Decision) -> dict[str, Any]:
    return asdict(decision)


def build_server(
    book: DecisionBook | None = None,
    *,
    load_tasks: Callable[[], list[TrackedTask]] = _load_tasks,
    cwd: Path | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_seconds: float = POLL_SECONDS,
) -> MCPServer:
    book = book or DecisionBook()
    here = cwd or Path.cwd()
    apps = Apps()

    async def task_of(ref: str | None) -> TrackedTask:
        tasks = await asyncio.to_thread(load_tasks)
        try:
            return resolve_task(ref, tasks, here)
        except (NoTask, AmbiguousTask) as error:
            raise ToolError(str(error)) from error

    async def wait_for_answer(decision_id: str, ctx: Context) -> str:
        polls = 0
        while True:
            try:
                decision = await asyncio.to_thread(book.get, decision_id)
            except NoSuchDecision:
                return (
                    "The user deleted this question without answering it. Carry on "
                    "without it, or ask it differently if you still need it."
                )
            if decision.status == "answered":
                await asyncio.to_thread(book.mark_delivered, decision_id)
                return describe_answers(decision)
            polls += 1
            if polls % PROGRESS_EVERY == 0:
                await ctx.report_progress(polls, None, "waiting for the user's answer")
            await sleep(poll_seconds)

    @apps.tool(
        resource_uri=APP_URI,
        description=(
            "Ask the user to decide. Records the decision points, shows them to the user "
            "and waits for their answers (an option, their own words, or both, for each "
            "point), which are the result. `task` is a branch, ticket or task id; leave it "
            "out for the task checked out here."
        ),
    )
    async def propose_decisions(
        title: str,
        points: list[PointInput],
        ctx: Context,
        task: str | None = None,
    ) -> str:
        try:
            parsed = points_from_data([p.model_dump() for p in points])
        except InvalidDecision as error:
            raise ToolError(str(error)) from error
        target = await task_of(task)
        decision = await asyncio.to_thread(
            lambda: book.propose(
                target,
                proposer="interactive",
                title=title,
                points=parsed,
                session_id=os.environ.get("CLAUDE_CODE_SESSION_ID"),
            )
        )
        return await wait_for_answer(decision.id, ctx)

    @apps.tool(
        resource_uri=APP_URI,
        visibility=["app"],
        description="For the decisions App: the newest question about the task.",
    )
    async def pending_decision(task: str | None = None) -> str:
        target = await task_of(task)
        found = await asyncio.to_thread(book.for_task, target.task_id)
        pending = [d for d in found if d.status == "pending"]
        latest = pending[-1] if pending else (found[-1] if found else None)
        return json.dumps(_decision_json(latest) if latest else None)

    @apps.tool(
        resource_uri=APP_URI,
        visibility=["app"],
        description="For the decisions App: the user's answers to a question.",
    )
    async def answer_decision(decision_id: str, answers: list[AnswerInput]) -> str:
        try:
            answered = await asyncio.to_thread(
                book.answer, decision_id, [a.model_dump() for a in answers]
            )
        except NoSuchDecision as error:
            raise ToolError("This question was deleted.") from error
        except AlreadyAnswered as error:
            raise ToolError("This question was answered already.") from error
        except InvalidDecision as error:
            raise ToolError(str(error)) from error
        return json.dumps(_decision_json(answered))

    apps.add_html_resource(APP_URI, APP_HTML.read_text(), title="Decisions")
    server = MCPServer(
        "toolbelt-decisions", instructions=INSTRUCTIONS, extensions=[apps]
    )

    @server.tool(
        description=(
            "Read the questions asked about a task and the user's answers to them, "
            "newest last (a question can be answered after its session closed). "
            "Answers read here count as delivered. `task` is a branch, ticket or task "
            "id; leave it out for the task checked out here."
        )
    )
    async def read_decisions(task: str | None = None) -> str:
        target = await task_of(task)
        found = await asyncio.to_thread(book.for_task, target.task_id)
        if not found:
            return f"No question was asked about {target.branch}."
        parts = []
        for decision in found:
            if decision.status == "answered":
                parts.append(describe_answers(decision))
                if decision.delivered_at is None:
                    await asyncio.to_thread(book.mark_delivered, decision.id)
            else:
                parts.append(
                    f"Decision: {decision.title}\n  (still waiting for the user's answer)"
                )
        return "\n\n".join(parts)

    return server
