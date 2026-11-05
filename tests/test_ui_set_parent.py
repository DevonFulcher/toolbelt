"""Changing a branch's stack parent from its card (`POST /api/set-parent`)."""

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ui import NOW, FakeStatuses, seed_repo

from toolbelt.ui.app import create_app
from toolbelt.ui.set_parent import SetParentOutcome, failure_reason


@pytest.fixture
def make_client():
    def make(**app_kwargs) -> TestClient:
        app_kwargs.setdefault("wall_clock", lambda: NOW)
        app_kwargs.setdefault("worktree_of", lambda repo, branch: repo / "wt" / branch)
        app = create_app(None, fetch=FakeStatuses(), tracker=lambda: None, **app_kwargs)
        return TestClient(app)

    return make


def wait_until(condition, what: str) -> None:
    for _ in range(300):
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def set_parent_info(client: TestClient, repo_id: int, name: str) -> dict | None:
    """What the card's extras say of the branch's parent, once they are in."""
    extras = client.get("/api/snapshot").json()["extras"]
    if extras is None:
        return None
    found = next(b for b in extras["branches"] if b["id"] == f"{repo_id}:{name}")
    return found["set_parent"]


def settled(client: TestClient, repo_id: int, name: str, **expected) -> bool:
    info = set_parent_info(client, repo_id, name)
    return info is not None and all(info[k] == v for k, v in expected.items())


def test_a_parent_change_is_accepted_at_once_and_run_in_the_branchs_worktree(
    tmp_path, make_client
):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "main"})
    calls: list[tuple[Path, str]] = []
    release = threading.Event()

    def runner(worktree: Path, new_parent: str) -> SetParentOutcome:
        calls.append((worktree, new_parent))
        release.wait(timeout=10)
        return SetParentOutcome(ok=True, output="Set parent of 'devon/b' to 'devon/a'.")

    with make_client(set_parenter=runner) as client:
        response = client.post(
            "/api/set-parent", json={"id": f"{repo_id}:devon/b", "parent": "devon/a"}
        )

        assert response.status_code == 202 and response.json() == {"setting": True}
        wait_until(lambda: calls, "the command")
        assert calls == [(tmp_path / "api" / "wt" / "devon/b", "devon/a")]
        # In progress on the card until the command ends, then nothing to say.
        wait_until(
            lambda: settled(client, repo_id, "devon/b", setting="devon/a"),
            "it to read in progress",
        )
        release.set()
        wait_until(
            lambda: settled(client, repo_id, "devon/b", setting=None, notice=None),
            "it to finish",
        )


def test_the_trunk_can_be_the_new_parent(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "devon/a"})
    calls: list[str] = []

    def runner(worktree: Path, new_parent: str) -> SetParentOutcome:
        calls.append(new_parent)
        return SetParentOutcome(ok=True, output="")

    with make_client(set_parenter=runner) as client:
        response = client.post(
            "/api/set-parent", json={"id": f"{repo_id}:devon/b", "parent": "main"}
        )

        assert response.status_code == 202
        wait_until(lambda: calls == ["main"], "the command")


def test_what_the_command_refuses_with_is_shown_on_the_branch(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "devon/a"})

    def runner(worktree: Path, new_parent: str) -> SetParentOutcome:
        return SetParentOutcome(
            ok=False,
            output="🔧 Setting 'devon/b' as parent of 'devon/a' would create a cycle.",
        )

    with make_client(set_parenter=runner) as client:
        response = client.post(
            "/api/set-parent", json={"id": f"{repo_id}:devon/a", "parent": "devon/b"}
        )

        assert response.status_code == 202
        wait_until(
            lambda: settled(
                client,
                repo_id,
                "devon/a",
                setting=None,
                notice="Setting 'devon/b' as parent of 'devon/a' would create a cycle.",
            ),
            "the command's reason",
        )


def test_a_new_attempt_clears_the_last_reason(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "main"})
    outcomes = [
        SetParentOutcome(ok=False, output="Branch 'nope' does not exist."),
        SetParentOutcome(ok=True, output=""),
    ]

    with make_client(set_parenter=lambda wt, parent: outcomes.pop(0)) as client:
        body = {"id": f"{repo_id}:devon/b", "parent": "nope"}
        client.post("/api/set-parent", json=body)
        wait_until(
            lambda: settled(
                client, repo_id, "devon/b", notice="Branch 'nope' does not exist."
            ),
            "the reason",
        )

        client.post("/api/set-parent", json={**body, "parent": "devon/a"})

        wait_until(
            lambda: settled(client, repo_id, "devon/b", setting=None, notice=None),
            "the reason to clear",
        )


def test_a_branch_that_is_not_tracked_is_a_404_and_nothing_runs(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    calls: list[str] = []

    def runner(worktree: Path, new_parent: str) -> SetParentOutcome:
        calls.append(new_parent)
        return SetParentOutcome(ok=True, output="")

    with make_client(set_parenter=runner) as client:
        response = client.post(
            "/api/set-parent", json={"id": f"{repo_id}:devon/zzz", "parent": "main"}
        )

        assert response.status_code == 404
        assert calls == []


def test_the_branch_it_already_hangs_off_is_refused_and_nothing_runs(
    tmp_path, make_client
):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    calls: list[str] = []

    def runner(worktree: Path, new_parent: str) -> SetParentOutcome:
        calls.append(new_parent)
        return SetParentOutcome(ok=True, output="")

    with make_client(set_parenter=runner) as client:
        response = client.post(
            "/api/set-parent", json={"id": f"{repo_id}:devon/a", "parent": "main"}
        )

        assert response.status_code == 409
        assert "already its parent" in response.json()["detail"]
        assert calls == []


def test_a_branch_checked_out_nowhere_is_refused_and_says_where_it_must_run(
    tmp_path, make_client
):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "main"})
    calls: list[str] = []

    def runner(worktree: Path, new_parent: str) -> SetParentOutcome:
        calls.append(new_parent)
        return SetParentOutcome(ok=True, output="")

    with make_client(
        set_parenter=runner, worktree_of=lambda repo, branch: None
    ) as client:
        response = client.post(
            "/api/set-parent", json={"id": f"{repo_id}:devon/b", "parent": "devon/a"}
        )

        assert response.status_code == 409
        assert "not checked out in any worktree" in response.json()["detail"]
        assert calls == []


def test_a_change_already_running_is_not_started_twice(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "main"})
    release = threading.Event()
    calls: list[str] = []

    def runner(worktree: Path, new_parent: str) -> SetParentOutcome:
        calls.append(new_parent)
        release.wait(timeout=10)
        return SetParentOutcome(ok=True, output="")

    with make_client(set_parenter=runner) as client:
        body = {"id": f"{repo_id}:devon/b", "parent": "devon/a"}
        assert client.post("/api/set-parent", json=body).status_code == 202

        again = client.post("/api/set-parent", json=body)

        assert again.status_code == 409
        release.set()
        wait_until(lambda: len(calls) == 1, "the one command")
        assert calls == ["devon/a"]


def test_a_failure_reason_is_the_end_of_what_the_command_said_without_its_echoes():
    output = (
        "🔧 git rev-parse --verify --quiet nope\n🔧 Branch 'nope' does not exist.\n"
    )

    assert failure_reason(output) == "Branch 'nope' does not exist."
    assert failure_reason("x" * 5000) == "x" * 600
