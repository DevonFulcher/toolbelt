"""A merged PR that received review comments stays on the page, as Acknowledge,
until the user acknowledges it (`/api/acknowledge`); one with no comments goes
away as Done does. Merging itself is never held up by comments."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.ui.app import create_app
from toolbelt.ui.pr_activity import PrFacts, ReviewComment
from toolbelt.ui.pr_fetch import PrSnapshot

COPILOT_REVIEW = ReviewComment(
    kind="review",
    author="copilot-pull-request-reviewer",
    body="Needs a closer look: the retry never gives up.",
    url="https://github.com/o/r/pull/1945#pullrequestreview-1",
)


def merged_facts(number: int, comments: tuple[ReviewComment, ...]) -> PrFacts:
    return PrFacts(
        number=number,
        title="Change",
        author="devon",
        base_ref="main",
        head_oid="h",
        state="MERGED",
        is_draft=False,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        merged_at=datetime(2026, 10, 2, tzinfo=UTC),
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=(),
        review_comments=comments,
    )


class Github:
    """What GitHub says about each branch's merged PR."""

    def __init__(self) -> None:
        self.comments: dict[str, tuple[ReviewComment, ...]] = {}

    async def fetch(self, branch: str, root: Path) -> PrSnapshot:
        number = {"a": 1945, "b": 7}[branch]
        status = BranchStatus(
            pr=PrState.MERGED,
            ci=CiState.SUCCESS,
            review=ReviewState.APPROVED,
            url=f"https://github.com/o/r/pull/{number}",
        )
        return PrSnapshot(status, merged_facts(number, self.comments.get(branch, ())))


def make_app(github: Github):
    app = create_app(
        fetch=github.fetch, wall_clock=lambda: datetime(2026, 10, 6, tzinfo=UTC)
    )
    asyncio.run(app.state.refresher.run_once())
    return app


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
    store.set_parent(repo_id, "b", "main")
    store.close()
    github = Github()
    github.comments["a"] = (COPILOT_REVIEW,)
    return repo_id, github


def status(client: TestClient, name: str) -> dict:
    branches = client.get("/api/snapshot").json()["branches"]
    return next(b for b in branches if b["name"] == name)["status"]


def test_a_merged_pr_with_a_review_body_only_stays_as_acknowledge_with_its_comments(
    rig,
):
    _, github = rig
    with TestClient(make_app(github)) as client:
        reviewed = status(client, "a")

        assert reviewed["next"]["kind"] == "acknowledge"
        assert reviewed["next"]["who"] == "you"
        assert reviewed["unacknowledged_comments"] == [
            {
                "kind": "review",
                "author": "copilot-pull-request-reviewer",
                "body": "Needs a closer look: the retry never gives up.",
                "url": "https://github.com/o/r/pull/1945#pullrequestreview-1",
            }
        ]


def test_a_merged_pr_without_comments_is_done(rig):
    _, github = rig
    with TestClient(make_app(github)) as client:
        quiet = status(client, "b")

        assert quiet["next"]["kind"] == "done"
        assert quiet["next"]["who"] == "none"
        assert quiet["unacknowledged_comments"] == []


def test_acknowledging_makes_it_done_and_tells_the_page(rig):
    repo_id, github = rig
    app = make_app(github)
    with TestClient(app) as client:
        events = app.state.hub.subscribe()

        response = client.post("/api/acknowledge", json={"id": f"{repo_id}:a"})

        assert response.status_code == 204
        assert status(client, "a")["next"]["kind"] == "done"
        assert status(client, "a")["unacknowledged_comments"] == []
        published = []
        while not events.empty():
            published.append(events.get_nowait())
        pushed = [
            payload["status"]["next"]["kind"]
            for name, payload in published
            if name == "status" and payload["branch_id"] == f"{repo_id}:a"
        ]
        assert pushed == ["done"]


def test_an_acknowledgement_survives_a_restart(rig):
    repo_id, github = rig
    with TestClient(make_app(github)) as client:
        client.post("/api/acknowledge", json={"id": f"{repo_id}:a"})

    with TestClient(make_app(github)) as restarted:
        assert status(restarted, "a")["next"]["kind"] == "done"


def test_the_acknowledgement_is_kept_by_pr_number_so_a_reused_branch_name_starts_afresh(
    rig,
):
    repo_id, github = rig
    with TestClient(make_app(github)) as client:
        client.post("/api/acknowledge", json={"id": f"{repo_id}:a"})
        store = StackStore(default_db_path())
        try:
            assert store.acknowledged_prs(repo_id) == frozenset({1945})
        finally:
            store.close()


def test_only_a_merged_pr_can_be_acknowledged(rig):
    repo_id, _github = rig

    async def open_pr(branch: str, root: Path) -> PrSnapshot:
        return PrSnapshot(
            BranchStatus(
                pr=PrState.OPEN,
                ci=CiState.SUCCESS,
                review=ReviewState.NONE,
                url="https://github.com/o/r/pull/1945",
            ),
            merged_facts(1945, (COPILOT_REVIEW,)),
        )

    app = create_app(fetch=open_pr)
    asyncio.run(app.state.refresher.run_once())
    with TestClient(app) as client:
        refused = client.post("/api/acknowledge", json={"id": f"{repo_id}:a"})
        missing = client.post("/api/acknowledge", json={"id": f"{repo_id}:nope"})

    assert refused.status_code == 409
    assert missing.status_code == 404
