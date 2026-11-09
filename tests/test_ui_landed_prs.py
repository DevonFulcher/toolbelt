"""A merged PR whose branch was removed (cleaned up by sync) but which received
review comments stays on the page as a reduced card until it is acknowledged;
one with no comments never shows. The PR's comments are fetched from GitHub by
number."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.ui.app import create_app

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def comments_payload(*, reviews: list[dict] | None = None) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "title": "Retry the upload",
                    "url": "https://github.com/o/r/pull/1945",
                    "author": {"login": "devon"},
                    "reviewThreads": {"nodes": []},
                    "reviews": {"nodes": reviews or []},
                    "comments": {"nodes": []},
                }
            }
        }
    }


COPILOT_REVIEW = {
    "submittedAt": "2026-10-05T16:08:00Z",
    "state": "COMMENTED",
    "body": "Needs a closer look: the retry never gives up. " * 20,
    "url": "https://github.com/o/r/pull/1945#pullrequestreview-1",
    "author": {"__typename": "Bot", "login": "copilot-pull-request-reviewer"},
}


class Github:
    """Answers the by-number comments lookup from canned PRs."""

    def __init__(self) -> None:
        self.by_number: dict[int, dict] = {}
        self.calls: list[tuple[str, int]] = []
        self.fail = False

    async def __call__(self, slug: str, number: int) -> dict:
        self.calls.append((slug, number))
        if self.fail:
            raise RuntimeError("GitHub is down")
        return self.by_number[number]


@pytest.fixture
def rig(tmp_path: Path):
    path = tmp_path / "r"
    path.mkdir()
    store = StackStore(default_db_path())
    repo_id = store.ensure_repo(
        RepoIdentity(
            git_common_dir=path / ".git", path=path, remote_url="git@github.com:o/r.git"
        )
    )
    store.close()
    github = Github()
    return repo_id, github


def land(repo_id: int, branch: str, pr: int | None, *, landed: bool = True) -> None:
    """A branch tracked, linked to its PR and removed by sync."""
    store = StackStore(default_db_path())
    store.set_parent(repo_id, branch, "main", task="RD-1", task_tracker="jira")
    store.close()
    if pr is not None:
        raw = sqlite3.connect(default_db_path())
        raw.execute(
            "UPDATE branches SET pr_number = ?, pr_node_id = ? WHERE name = ?",
            (pr, f"PR_{pr}", branch),
        )
        raw.commit()
        raw.close()
    store = StackStore(default_db_path())
    store.remove_branch(repo_id, branch, landed=landed)
    store.close()


def make_app(github: Github):
    return create_app(pr_comments=github, wall_clock=lambda: NOW, fetch=_no_lookup)


async def _no_lookup(branch, root):
    from toolbelt.ui.pr_fetch import NO_SNAPSHOT

    return NO_SNAPSHOT


def refresh(app) -> None:
    asyncio.run(app.state.landed_feed.run_once())


def cards(client: TestClient) -> list[dict]:
    return client.get("/api/snapshot").json()["landed_prs"]


def test_a_removed_branchs_merged_pr_with_a_review_body_only_shows_as_a_card(rig):
    repo_id, github = rig
    github.by_number[1945] = comments_payload(reviews=[COPILOT_REVIEW])
    land(repo_id, "devon/retry", 1945)
    app = make_app(github)
    refresh(app)

    with TestClient(app) as client:
        [card] = cards(client)

    assert github.calls == [("o/r", 1945)]
    assert (card["id"], card["repo"], card["branch"], card["task"]) == (
        f"{repo_id}:1945",
        "r",
        "devon/retry",
        "RD-1",
    )
    assert (card["pr_number"], card["title"]) == (1945, "Retry the upload")
    assert card["pr_url"] == "https://github.com/o/r/pull/1945"
    [comment] = card["comments"]
    assert comment["author"] == "copilot-pull-request-reviewer"
    assert comment["kind"] == "review"
    assert comment["body"].startswith("Needs a closer look")
    assert len(comment["body"]) == 600


def test_acknowledging_it_removes_the_card_and_it_stays_gone_after_a_restart(rig):
    repo_id, github = rig
    github.by_number[1945] = comments_payload(reviews=[COPILOT_REVIEW])
    land(repo_id, "devon/retry", 1945)
    app = make_app(github)
    refresh(app)
    with TestClient(app) as client:
        events = app.state.hub.subscribe()

        response = client.post(
            "/api/acknowledge", json={"id": f"{repo_id}:devon/retry", "pr_number": 1945}
        )

        assert response.status_code == 204
        assert cards(client) == []
        published = []
        while not events.empty():
            published.append(events.get_nowait())
        assert [p["landed_prs"] for name, p in published if name == "landed_prs"] == [
            []
        ]

    restarted = make_app(github)
    refresh(restarted)
    with TestClient(restarted) as client:
        assert cards(client) == []


def test_a_merged_pr_without_comments_never_shows_and_is_not_looked_up_again(rig):
    repo_id, github = rig
    github.by_number[1945] = comments_payload()
    land(repo_id, "devon/quiet", 1945)
    app = make_app(github)

    refresh(app)
    refresh(app)

    with TestClient(app) as client:
        assert cards(client) == []
    assert github.calls == [("o/r", 1945)]
    store = StackStore(default_db_path())
    assert store.acknowledged_prs(repo_id) == frozenset({1945})


def test_only_landed_removals_with_a_pr_number_within_thirty_days_are_considered(rig):
    repo_id, github = rig
    github.by_number[1] = comments_payload(reviews=[COPILOT_REVIEW])
    land(repo_id, "devon/old", 1)
    land(repo_id, "devon/abandoned", 2, landed=False)
    land(repo_id, "devon/unnumbered", None)
    stale = (NOW - timedelta(days=31)).isoformat()
    raw = sqlite3.connect(default_db_path())
    raw.execute("UPDATE branches SET removed_at = ? WHERE name = 'devon/old'", (stale,))
    raw.commit()
    raw.close()
    app = make_app(github)

    refresh(app)

    assert github.calls == []
    with TestClient(app) as client:
        assert cards(client) == []


def test_a_failed_lookup_is_retried_and_does_not_acknowledge(rig):
    repo_id, github = rig
    github.by_number[1945] = comments_payload(reviews=[COPILOT_REVIEW])
    land(repo_id, "devon/retry", 1945)
    app = make_app(github)
    github.fail = True
    refresh(app)
    github.fail = False

    refresh(app)

    with TestClient(app) as client:
        assert len(cards(client)) == 1
    assert len(github.calls) == 2


def test_only_a_shown_landed_pr_can_be_acknowledged_this_way(rig):
    repo_id, github = rig
    app = make_app(github)
    with TestClient(app) as client:
        response = client.post(
            "/api/acknowledge", json={"id": f"{repo_id}:devon/x", "pr_number": 7}
        )

    assert response.status_code == 404
