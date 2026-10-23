"""`/api/decisions` through the app: the questions of a branch's task, answering
and deleting them, and the "Deciding" state of the card that follows."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolbelt.decisions.book import DecisionBook
from toolbelt.decisions.model import points_from_data
from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.task_claims.resolution import TrackedTask
from toolbelt.ui.app import create_app
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.pr_fetch import PrSnapshot

POINTS = points_from_data(
    [
        {
            "id": "D1",
            "title": "Where?",
            "context": "Two places.",
            "options": [
                {"id": "O1", "title": "Store", "summary": "One table."},
                {"id": "O2", "title": "Module"},
            ],
            "recommendation": {"option_id": "O1", "reasoning": "Simpler."},
        }
    ]
)


def facts() -> PrFacts:
    return PrFacts(
        number=1,
        title="DI-1: Change a",
        author="devon",
        base_ref="main",
        head_oid="abc",
        state="OPEN",
        is_draft=False,
        created_at=datetime.now(UTC) - timedelta(days=2),
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=(),
        additions=20,
        deletions=4,
    )


@pytest.fixture
def rig(tmp_path: Path):
    import asyncio

    store = StackStore(default_db_path())
    path = tmp_path / "r"
    path.mkdir()
    repo_id = store.ensure_repo(
        RepoIdentity(
            git_common_dir=path / ".git", path=path, remote_url="git@github.com:o/r.git"
        )
    )
    store.set_parent(repo_id, "a", "main")
    task_id = next(f.task_id for f in store.tracked_branch_facts() if f.name == "a")
    store.close()

    async def fetch(branch: str, root: Path) -> PrSnapshot:
        status = BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.FAILED,
            review=ReviewState.NONE,
            url="https://github.com/o/r/pull/1",
        )
        return PrSnapshot(status, facts())

    app = create_app(fetch=fetch, tick=0.05)
    asyncio.run(app.state.refresher.run_once())
    task = TrackedTask(repo_id, "r", "a", task_id, None, None)
    with TestClient(app) as client:
        # A page is connected: the database watcher only runs then.
        app.state.hub.subscribe()
        yield client, repo_id, task


def next_of(client, branch: str = "a") -> dict:
    found = {b["name"]: b for b in client.get("/api/snapshot").json()["branches"]}
    return found[branch]["status"]["next"]


def propose(task: TrackedTask):
    return DecisionBook().propose(
        task, proposer="interactive", title="Storage", points=POINTS, session_id="s-1"
    )


def wait_for(condition, what: str) -> None:
    import time

    for _ in range(300):
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_question_proposed_elsewhere_puts_the_task_in_deciding(rig):
    client, _repo_id, task = rig
    assert next_of(client)["kind"] == "fix_ci"

    propose(task)  # another process (the MCP server) writes the database

    wait_for(lambda: next_of(client)["kind"] == "deciding", "the card to say Deciding")
    assert next_of(client)["would_be"]["kind"] == "fix_ci"


def test_the_questions_of_a_branch_are_listed_with_their_points(rig):
    client, repo_id, task = rig
    decision = propose(task)

    body = client.get("/api/decisions", params={"id": f"{repo_id}:a"}).json()

    (found,) = body["decisions"]
    assert (found["id"], found["status"], found["proposer"]) == (
        decision.id,
        "pending",
        "interactive",
    )
    assert found["points"][0]["options"][0]["summary"] == "One table."
    assert found["points"][0]["recommendation"] == {
        "option_id": "O1",
        "reasoning": "Simpler.",
    }
    assert client.get("/api/decisions", params={"id": f"{repo_id}:other"}).json() == {
        "decisions": []
    }


def test_answering_records_it_and_the_task_stops_deciding(rig):
    client, _repo_id, task = rig
    decision = propose(task)
    wait_for(lambda: next_of(client)["kind"] == "deciding", "Deciding")

    response = client.post(
        "/api/decisions/answer",
        json={
            "decision_id": decision.id,
            "answers": [{"point_id": "D1", "option_id": "O2", "text": "but soon"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "answered"
    assert response.json()["answers"] == [
        {"point_id": "D1", "option_id": "O2", "text": "but soon"}
    ]
    assert next_of(client)["kind"] == "fix_ci"


def test_an_answer_that_does_not_fit_is_a_422_and_a_second_answer_a_409(rig):
    client, _repo_id, task = rig
    decision = propose(task)
    bad = {
        "decision_id": decision.id,
        "answers": [{"point_id": "D1", "option_id": "Z", "text": ""}],
    }
    good = {
        "decision_id": decision.id,
        "answers": [{"point_id": "D1", "option_id": "O1", "text": ""}],
    }

    assert client.post("/api/decisions/answer", json=bad).status_code == 422
    assert client.post("/api/decisions/answer", json=good).status_code == 200
    assert client.post("/api/decisions/answer", json=good).status_code == 409
    assert (
        client.post(
            "/api/decisions/answer", json={**good, "decision_id": "nope"}
        ).status_code
        == 404
    )


def test_a_decision_can_be_answered_after_its_session_is_gone_and_read_back(rig):
    client, _repo_id, task = rig
    decision = propose(task)  # the session that asked has ended since

    client.post(
        "/api/decisions/answer",
        json={
            "decision_id": decision.id,
            "answers": [{"point_id": "D1", "option_id": "O1", "text": ""}],
        },
    )

    (read,) = DecisionBook().for_task(task.task_id)  # what a read tool returns
    assert (read.status, read.answers[0].option_id) == ("answered", "O1")
    assert read.delivered_at is None  # nobody has taken it yet


def test_deleting_a_question_removes_it_and_the_task_stops_deciding(rig):
    client, repo_id, task = rig
    decision = propose(task)
    wait_for(lambda: next_of(client)["kind"] == "deciding", "Deciding")

    response = client.post("/api/decisions/delete", json={"decision_id": decision.id})

    assert response.status_code == 200
    assert client.get("/api/decisions", params={"id": f"{repo_id}:a"}).json() == {
        "decisions": []
    }
    assert next_of(client)["kind"] == "fix_ci"
    assert (
        client.post(
            "/api/decisions/delete", json={"decision_id": decision.id}
        ).status_code
        == 404
    )
