"""`/api/merge/queue` through the app: a PR waiting on CI is queued, the queue
merges it once the CI has passed, and the cards say where it stands. The merge
itself (`toolbelt git merge`) is faked."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.ui.app import create_app
from toolbelt.ui.merge_pr import MergeOutcome
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.pr_fetch import PrSnapshot


def facts(head: str) -> PrFacts:
    return PrFacts(
        number=1,
        title="Change a",
        author="devon",
        base_ref="main",
        head_oid=head,
        state="OPEN",
        is_draft=False,
        created_at=datetime.now(UTC) - timedelta(days=2),
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=(),
    )


class Github:
    """What GitHub says about branch a's PR: changed by the tests."""

    def __init__(self) -> None:
        self.ci = CiState.RUNNING
        self.review = ReviewState.APPROVED
        self.head = "h1"

    async def fetch(self, branch: str, root: Path) -> PrSnapshot:
        status = BranchStatus(
            pr=PrState.OPEN,
            ci=self.ci,
            review=self.review,
            url="https://github.com/o/r/pull/1",
        )
        return PrSnapshot(status, facts(self.head))


@pytest.fixture
def rig(tmp_path: Path):
    store = StackStore(default_db_path())
    path = tmp_path / "r"
    path.mkdir()
    repo_id = store.ensure_repo(
        RepoIdentity(
            git_common_dir=path / ".git", path=path, remote_url="git@github.com:o/r.git"
        )
    )
    store.set_parent(repo_id, "a", "main")
    store.close()
    github = Github()
    merges: list[tuple[Path, str]] = []

    def merger(repo_path: Path, pr_url: str) -> MergeOutcome:
        merges.append((repo_path, pr_url))
        return MergeOutcome(ok=True, output="merged")

    app = create_app(
        fetch=github.fetch,
        merger=merger,
        merge_queue_tick=0.02,
        refresh_interval=0.01,  # what GitHub says is looked up again at once
    )
    asyncio.run(app.state.refresher.run_once())
    with TestClient(app) as client:
        yield app, repo_id, github, merges, client


def refresh(app, client) -> None:
    client.portal.call(app.state.refresher.run_once)


def branch(client, name: str = "a") -> dict:
    return next(
        b for b in client.get("/api/snapshot").json()["branches"] if b["name"] == name
    )


def wait_for(condition, what: str) -> None:
    for _ in range(300):
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_pr_waiting_only_on_ci_can_be_queued_and_says_so(rig):
    _, _, _, _, client = rig

    queue = branch(client)["merge_queue"]

    assert branch(client)["status"]["next"]["kind"] == "wait_ci"
    assert queue["can_queue"] is True and queue["queued"] is False


def test_queueing_it_marks_it_queued(rig):
    _, repo_id, _, _, client = rig

    response = client.post("/api/merge/queue", json={"id": f"{repo_id}:a"})

    assert response.status_code == 200
    assert response.json()["queued"] is True
    queue = branch(client)["merge_queue"]
    assert queue["queued"] is True and queue["can_queue"] is False


def test_the_queue_merges_the_pr_once_its_ci_has_passed(rig):
    app, repo_id, github, merges, client = rig
    client.post("/api/merge/queue", json={"id": f"{repo_id}:a"})
    time.sleep(0.1)
    assert merges == []  # CI is still running

    github.ci = CiState.SUCCESS
    refresh(app, client)

    wait_for(lambda: merges, "the queued merge")
    assert merges == [(merges[0][0], "https://github.com/o/r/pull/1")]
    wait_for(lambda: branch(client)["merge_queue"] is None, "the queue to empty")


def test_nothing_is_merged_that_was_not_queued(rig):
    app, _, github, merges, client = rig

    github.ci = CiState.SUCCESS
    refresh(app, client)
    time.sleep(0.15)

    assert merges == []


def test_a_pr_with_new_commits_is_dropped_with_a_note_and_not_merged(rig):
    app, repo_id, github, merges, client = rig
    client.post("/api/merge/queue", json={"id": f"{repo_id}:a"})

    github.head = "h2"
    github.ci = CiState.SUCCESS
    refresh(app, client)

    wait_for(
        lambda: (branch(client)["merge_queue"] or {}).get("notice"),
        "the note on the card",
    )
    assert "New commits" in branch(client)["merge_queue"]["notice"]
    assert merges == []


def test_a_pr_whose_ci_failed_is_taken_off(rig):
    app, repo_id, github, merges, client = rig
    client.post("/api/merge/queue", json={"id": f"{repo_id}:a"})

    github.ci = CiState.FAILED
    refresh(app, client)

    wait_for(
        lambda: (branch(client)["merge_queue"] or {}).get("notice"),
        "the note on the card",
    )
    assert "CI failed" in branch(client)["merge_queue"]["notice"]
    assert merges == []


def test_a_pr_that_is_not_waiting_on_ci_cannot_be_queued(rig):
    app, repo_id, github, _, client = rig
    github.review = ReviewState.REQUESTED  # not approved
    refresh(app, client)

    response = client.post("/api/merge/queue", json={"id": f"{repo_id}:a"})

    assert response.status_code == 409 and "approved" in response.json()["detail"]
    assert branch(client)["merge_queue"] is None


def test_an_unknown_branch_is_404(rig):
    _, repo_id, _, _, client = rig

    assert (
        client.post("/api/merge/queue", json={"id": f"{repo_id}:nope"}).status_code
        == 404
    )


def test_cancelling_takes_it_off_the_queue(rig):
    app, repo_id, github, merges, client = rig
    client.post("/api/merge/queue", json={"id": f"{repo_id}:a"})

    response = client.post("/api/merge/unqueue", json={"id": f"{repo_id}:a"})
    github.ci = CiState.SUCCESS
    refresh(app, client)
    time.sleep(0.15)

    assert response.status_code == 204
    assert merges == []
    assert StackStore(default_db_path()).merge_queue() == []


def test_the_queue_is_kept_in_the_database(rig):
    _, repo_id, _, _, client = rig

    client.post("/api/merge/queue", json={"id": f"{repo_id}:a"})

    rows = StackStore(default_db_path()).merge_queue()
    assert [(r.repo_id, r.branch, r.pr_number, r.head_oid) for r in rows] == [
        (repo_id, "a", 1, "h1")
    ]


def test_the_cards_hear_when_it_is_queued(rig):
    app, repo_id, _, _, client = rig
    queue = app.state.hub.subscribe()

    client.post("/api/merge/queue", json={"id": f"{repo_id}:a"})

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    states = [
        data["merge_queue"]["queued"]
        for name, data in events
        if name == "status" and data["merge_queue"] is not None
    ]
    assert True in states
