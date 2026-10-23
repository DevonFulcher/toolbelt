"""The MCP server for interactive sessions, run in-process with the SDK's own
client: what it offers, how a question waits for its answer, and how a later
session reads one."""

import asyncio
import json
from pathlib import Path
from typing import ClassVar

import pytest
from mcp import Client

from toolbelt.decisions.book import DecisionBook
from toolbelt.decisions.mcp_server import APP_URI, build_server
from toolbelt.decisions.tasks import AmbiguousTask, NoTask, resolve_task
from toolbelt.task_claims.resolution import TrackedTask

TASK = TrackedTask(1, "api", "devon/a", "task-a", "RD-1", Path("/wt/a"))
OTHER = TrackedTask(2, "web", "devon/a", "task-b", None, Path("/wt/b"))

POINTS = [
    {
        "id": "D1",
        "title": "Where?",
        "options": [
            {"id": "O1", "title": "Store", "summary": "One table."},
            {"id": "O2", "title": "Module"},
        ],
        "recommendation": {"option_id": "O1", "reasoning": "Simpler."},
    }
]
ANSWER = [{"point_id": "D1", "option_id": "O1", "text": "ok"}]


def server(tasks=(TASK,)):
    book = DecisionBook()

    async def quick(seconds: float) -> None:
        await asyncio.sleep(0.01)

    return book, build_server(
        book, load_tasks=lambda: list(tasks), poll_seconds=0.01, sleep=quick
    )


def text_of(result) -> str:
    return "".join(c.text for c in result.content if c.type == "text")


async def pending_id(book: DecisionBook, task_id: str = "task-a") -> str:
    for _ in range(300):
        found = [d for d in book.for_task(task_id) if d.status == "pending"]
        if found:
            return found[-1].id
        await asyncio.sleep(0.01)
    raise AssertionError("no question was recorded")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_the_tools_are_offered_with_the_app_bound_to_the_ones_that_show_it():
    async def scenario():
        _book, mcp = server()
        async with Client(mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            resource = await client.read_resource(APP_URI)

        assert set(tools) == {
            "propose_decisions",
            "read_decisions",
            "pending_decision",
            "answer_decision",
        }
        assert tools["propose_decisions"].meta["ui"]["resourceUri"] == APP_URI
        assert tools["answer_decision"].meta["ui"]["visibility"] == ["app"]
        assert tools["pending_decision"].meta["ui"]["visibility"] == ["app"]
        assert "ui" not in (tools["read_decisions"].meta or {})
        (content,) = resource.contents
        assert content.mime_type == "text/html;profile=mcp-app"
        assert "ui/initialize" in content.text

    run(scenario())


def test_a_proposal_waits_until_the_app_answers_it_and_returns_the_answers():
    async def scenario():
        book, mcp = server()
        async with Client(mcp) as client:
            call = asyncio.ensure_future(
                client.call_tool(
                    "propose_decisions",
                    {"title": "Storage", "points": POINTS, "task": "devon/a"},
                )
            )
            decision_id = await pending_id(book)
            await asyncio.sleep(0.05)
            assert not call.done()  # still waiting

            seen = json.loads(
                text_of(await client.call_tool("pending_decision", {"task": "devon/a"}))
            )
            assert seen["id"] == decision_id and seen["status"] == "pending"
            await client.call_tool(
                "answer_decision", {"decision_id": decision_id, "answers": ANSWER}
            )
            result = await call

        text = text_of(result)
        assert "Chosen: O1 (Store)" in text and "In the user's words: ok" in text
        assert book.get(decision_id).delivered_at is not None

    run(scenario())


def test_an_answer_given_in_toolbelts_own_ui_ends_the_wait_too():
    async def scenario():
        book, mcp = server()
        async with Client(mcp) as client:
            call = asyncio.ensure_future(
                client.call_tool(
                    "propose_decisions",
                    {"title": "Storage", "points": POINTS, "task": "devon/a"},
                )
            )
            book.answer(await pending_id(book), ANSWER)
            result = await call

        assert "Chosen: O1" in text_of(result)

    run(scenario())


def test_a_question_deleted_while_waiting_ends_the_wait_with_an_explanation():
    async def scenario():
        book, mcp = server()
        async with Client(mcp) as client:
            call = asyncio.ensure_future(
                client.call_tool(
                    "propose_decisions",
                    {"title": "Storage", "points": POINTS, "task": "devon/a"},
                )
            )
            book.delete(await pending_id(book))
            result = await call

        assert "deleted this question without answering" in text_of(result)

    run(scenario())


def test_points_that_do_not_fit_are_refused_before_anything_is_recorded():
    async def scenario():
        book, mcp = server()
        async with Client(mcp) as client:
            result = await client.call_tool(
                "propose_decisions",
                {
                    "title": "Storage",
                    "task": "devon/a",
                    "points": [{"id": "D1", "title": "x"}, {"id": "D1", "title": "y"}],
                },
            )

        assert result.is_error
        assert "unique" in text_of(result)
        assert book.all() == []

    run(scenario())


def test_a_later_session_reads_the_questions_of_a_task_and_their_answers():
    async def scenario():
        book, mcp = server()
        async with Client(mcp) as client:
            asked = book.propose(
                TASK, proposer="interactive", title="Storage", points=_points()
            )
            book.propose(TASK, proposer="interactive", title="Naming", points=_points())
            book.answer(asked.id, ANSWER)  # answered after its session closed

            result = await client.call_tool("read_decisions", {"task": "RD-1"})

        text = text_of(result)
        assert "Decision: Storage" in text and "Chosen: O1 (Store)" in text
        assert "Decision: Naming" in text and "still waiting" in text
        assert book.get(asked.id).delivered_at is not None  # now read by an agent

    run(scenario())


def test_a_task_nobody_asked_about_says_so():
    async def scenario():
        _book, mcp = server()
        async with Client(mcp) as client:
            result = await client.call_tool("read_decisions", {"task": "devon/a"})

        assert "No question was asked" in text_of(result)

    run(scenario())


def test_the_app_cannot_answer_a_question_twice_or_with_the_wrong_points():
    async def scenario():
        book, mcp = server()
        async with Client(mcp) as client:
            decision = book.propose(
                TASK, proposer="interactive", title="Storage", points=_points()
            )
            bad = await client.call_tool(
                "answer_decision",
                {
                    "decision_id": decision.id,
                    "answers": [{"point_id": "D9", "text": "x"}],
                },
            )
            await client.call_tool(
                "answer_decision", {"decision_id": decision.id, "answers": ANSWER}
            )
            again = await client.call_tool(
                "answer_decision", {"decision_id": decision.id, "answers": ANSWER}
            )

        assert bad.is_error and "D1 is not answered" in text_of(bad)
        assert again.is_error and "answered already" in text_of(again)

    run(scenario())


def _points():
    from toolbelt.decisions.model import points_from_data

    return points_from_data(POINTS)


class TestWhichTask:
    tasks: ClassVar[list[TrackedTask]] = [TASK, OTHER]

    def test_a_ref_names_a_branch_a_ticket_or_a_task_id(self):
        cwd = Path("/x")

        assert resolve_task("RD-1", self.tasks, cwd) is TASK
        assert resolve_task("task-b", self.tasks, cwd) is OTHER

    def test_a_branch_in_two_repos_is_ambiguous(self):
        with pytest.raises(AmbiguousTask, match="api:devon/a, web:devon/a"):
            resolve_task("devon/a", self.tasks, Path("/x"))

    def test_an_unknown_ref_is_refused(self):
        with pytest.raises(NoTask, match="Name the branch or ticket"):
            resolve_task("nope", self.tasks, Path("/x"))

    def test_without_a_ref_it_is_the_task_checked_out_where_the_session_runs(self):
        found = resolve_task(None, self.tasks, Path("/wt/b"), lambda cwd: Path("/wt/b"))

        assert found is OTHER

    def test_outside_any_tracked_worktree_there_is_no_task(self):
        with pytest.raises(NoTask, match="checked out in /elsewhere"):
            resolve_task(
                None, self.tasks, Path("/elsewhere"), lambda cwd: Path("/elsewhere")
            )
        with pytest.raises(NoTask):
            resolve_task(None, self.tasks, Path("/elsewhere"), lambda cwd: None)
