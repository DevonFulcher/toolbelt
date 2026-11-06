"""Removing a branch from its card (`POST /api/remove-branch`)."""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_ui import NOW, FakeStatuses, seed_repo

from toolbelt.ui.app import create_app
from toolbelt.ui.remove_branch import RemoveOutcome, RemoveRequest


@pytest.fixture
def make_client():
    def make(**app_kwargs) -> TestClient:
        app_kwargs.setdefault("wall_clock", lambda: NOW)
        app = create_app(None, fetch=FakeStatuses(), tracker=lambda: None, **app_kwargs)
        return TestClient(app)

    return make


def wait_until(condition, what: str) -> None:
    for _ in range(300):
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def remove_info(client: TestClient, repo_id: int, name: str) -> dict | None:
    """What the card's extras say of removing the branch, once they are in."""
    extras = client.get("/api/snapshot").json()["extras"]
    if extras is None:
        return None
    found = next(b for b in extras["branches"] if b["id"] == f"{repo_id}:{name}")
    return found["remove"]


def settled(client: TestClient, repo_id: int, name: str, **expected) -> bool:
    info = remove_info(client, repo_id, name)
    return info is not None and all(info[k] == v for k, v in expected.items())


def test_a_removal_is_accepted_at_once_and_run_in_the_repos_main_checkout(
    tmp_path, make_client
):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "devon/a"})
    calls: list[RemoveRequest] = []
    release = threading.Event()

    def runner(request: RemoveRequest) -> RemoveOutcome:
        calls.append(request)
        release.wait(timeout=10)
        return RemoveOutcome(ok=True, output="")

    with make_client(remover=runner) as client:
        response = client.post(
            "/api/remove-branch",
            json={"id": f"{repo_id}:devon/a", "reason": "  superseded  "},
        )

        assert response.status_code == 202 and response.json() == {"removing": True}
        wait_until(lambda: calls, "the command")
        assert calls == [
            RemoveRequest(
                repo_path=tmp_path / "api",
                branch="devon/a",
                force=False,
                reason="superseded",
            )
        ]
        wait_until(
            lambda: settled(client, repo_id, "devon/a", removing=True),
            "it to read in progress",
        )
        release.set()
        wait_until(
            lambda: settled(client, repo_id, "devon/a", removing=False, notice=None),
            "it to finish",
        )


def test_the_card_says_where_the_children_go(tmp_path, make_client):
    repo_id = seed_repo(
        tmp_path, "api", {"devon/a": "main", "devon/c": "devon/a", "devon/b": "devon/a"}
    )

    with make_client() as client:
        wait_until(
            lambda: settled(
                client,
                repo_id,
                "devon/a",
                parent="main",
                children=["devon/b", "devon/c"],
            ),
            "the card's removal info",
        )


def test_force_is_passed_on_and_a_blank_reason_is_none(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    calls: list[RemoveRequest] = []

    def runner(request: RemoveRequest) -> RemoveOutcome:
        calls.append(request)
        return RemoveOutcome(ok=True, output="")

    with make_client(remover=runner) as client:
        client.post(
            "/api/remove-branch",
            json={"id": f"{repo_id}:devon/a", "force": True, "reason": "   "},
        )

        wait_until(lambda: calls, "the command")
        assert calls[0].force is True and calls[0].reason is None


def test_what_the_command_refuses_with_is_shown_on_the_branch_until_the_next_try(
    tmp_path, make_client
):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    outcomes = [
        RemoveOutcome(
            ok=False,
            output="🔧 git worktree remove x\n🔧 Worktree has uncommitted changes.",
        ),
        RemoveOutcome(ok=True, output=""),
    ]

    with make_client(remover=lambda request: outcomes.pop(0)) as client:
        body = {"id": f"{repo_id}:devon/a"}
        client.post("/api/remove-branch", json=body)
        wait_until(
            lambda: settled(
                client,
                repo_id,
                "devon/a",
                removing=False,
                notice="Worktree has uncommitted changes.",
            ),
            "the command's reason",
        )

        client.post("/api/remove-branch", json={**body, "force": True})

        wait_until(
            lambda: settled(client, repo_id, "devon/a", removing=False, notice=None),
            "the reason to clear",
        )


def test_a_branch_that_is_not_tracked_is_a_404_and_nothing_runs(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    calls: list[RemoveRequest] = []

    def runner(request: RemoveRequest) -> RemoveOutcome:
        calls.append(request)
        return RemoveOutcome(ok=True, output="")

    with make_client(remover=runner) as client:
        response = client.post(
            "/api/remove-branch", json={"id": f"{repo_id}:devon/zzz"}
        )

        assert response.status_code == 404
        assert calls == []


def test_a_removal_already_running_is_not_started_twice(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    release = threading.Event()
    calls: list[RemoveRequest] = []

    def runner(request: RemoveRequest) -> RemoveOutcome:
        calls.append(request)
        release.wait(timeout=10)
        return RemoveOutcome(ok=True, output="")

    with make_client(remover=runner) as client:
        body = {"id": f"{repo_id}:devon/a"}
        assert client.post("/api/remove-branch", json=body).status_code == 202

        again = client.post("/api/remove-branch", json=body)

        assert again.status_code == 409
        release.set()
        wait_until(lambda: len(calls) == 1, "the one command")
