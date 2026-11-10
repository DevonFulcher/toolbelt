"""`/api/slack/draft` and `/api/slack/send` through the app: a branch's draft,
posting it, the recording and the events that follow. The Slack sender is
faked: nothing here reaches Slack."""

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.ui.app import create_app
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.pr_fetch import PrSnapshot
from toolbelt.ui.publish_pr import PublishOutcome
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_config import AssignRule, SlackConfig
from toolbelt.ui.slack_proxy import SentMessage, SlackProxyError

ALAN, WILL = Person("alan", "UA"), Person("will", "UW")
CONFIG = SlackConfig(
    channel="DTEST",
    reviewers=(WILL, ALAN),
    rules=(AssignRule(to="alan"),),
)


class FakeProxy:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.publish_calls: list[tuple[Path, str, int, bool]] = []
        self.fail: str | None = None

    async def send(self, *, channel, text, thread_ts=None) -> SentMessage:
        if self.fail:
            raise SlackProxyError(self.fail)
        self.sent.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        ts = f"1792600{len(self.sent):03d}.000100"
        return SentMessage(
            link=f"https://s.slack.com/archives/DTEST/p{ts.replace('.', '')}",
            ts=ts,
            thread_ts=thread_ts or ts,
            read_back=text,
            cost_usd=0.04,
        )


class FakePublisher:
    def __init__(self, calls: list[tuple[Path, str, int, bool]]) -> None:
        self.calls = calls

    def __call__(
        self, repo_root: Path, branch: str, pr_number: int, *, is_draft: bool
    ) -> PublishOutcome:
        self.calls.append((repo_root, branch, pr_number, is_draft))
        return PublishOutcome(True, "compressed")


def facts(number: int, title: str, *, is_draft: bool = False) -> PrFacts:
    return PrFacts(
        number=number,
        title=title,
        author="devon",
        base_ref="main",
        head_oid="abc",
        state="OPEN",
        is_draft=is_draft,
        created_at=datetime.now(UTC) - timedelta(days=2),
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=(),
        additions=20,
        deletions=4,
    )


def status(number: int, *, is_draft: bool = False) -> BranchStatus:
    return BranchStatus(
        pr=PrState.DRAFT if is_draft else PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.NONE,
        url=f"https://github.com/o/r/pull/{number}",
    )


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
    store.set_parent(repo_id, "b", "a")
    store.close()
    numbers = {"a": 1, "b": 2}

    async def fetch(branch: str, root: Path) -> PrSnapshot:
        number = numbers[branch]
        return PrSnapshot(
            status(number), facts(number, f"DI-{number}: Change {branch}")
        )

    proxy = FakeProxy()
    app = create_app(
        fetch=fetch,
        slack_settings=CONFIG,
        slack_proxy=proxy,  # type: ignore[arg-type]
        publisher=FakePublisher(proxy.publish_calls),
        tick=0.05,  # a change another process makes to the database is seen at once
    )
    asyncio.run(app.state.refresher.run_once())
    # In a ``with`` so the app's event loop outlives a request: a post runs in
    # the background after its request has been answered.
    with TestClient(app) as client:
        yield app, repo_id, proxy, client


@pytest.fixture
def draft_rig(tmp_path: Path):
    store = StackStore(default_db_path())
    path = tmp_path / "r"
    path.mkdir()
    repo_id = store.ensure_repo(
        RepoIdentity(
            git_common_dir=path / ".git", path=path, remote_url="git@github.com:o/r.git"
        )
    )
    store.set_parent(repo_id, "a", "main")
    store.set_parent(repo_id, "b", "a")
    store.close()

    async def fetch(branch: str, root: Path) -> PrSnapshot:
        number = {"a": 1, "b": 2}[branch]
        is_draft = True
        return PrSnapshot(
            status(number, is_draft=is_draft),
            facts(number, f"DI-{number}: Change {branch}", is_draft=is_draft),
        )

    proxy = FakeProxy()
    app = create_app(
        fetch=fetch,
        slack_settings=CONFIG,
        slack_proxy=proxy,  # type: ignore[arg-type]
        publisher=FakePublisher(proxy.publish_calls),
        tick=0.05,
    )
    asyncio.run(app.state.refresher.run_once())
    with TestClient(app) as client:
        yield app, repo_id, proxy, client


def wait_until_posted(client, branch_id: str) -> dict:
    """The branch's Slack info once its background post has finished."""
    for _ in range(200):
        branch = next(
            b
            for b in client.get("/api/snapshot").json()["branches"]
            if b["id"] == branch_id
        )
        if not branch["slack"]["posting"]:
            return branch["slack"]
        time.sleep(0.01)
    raise AssertionError("the post did not finish")


def branches(client) -> dict:
    return {b["name"]: b for b in client.get("/api/snapshot").json()["branches"]}


def test_both_branches_of_the_stack_say_post_in_slack(rig):
    _, _, _, client = rig

    got = branches(client)

    assert got["a"]["status"]["next"]["kind"] == "post_slack"
    assert got["b"]["status"]["next"]["kind"] == "post_slack"


def test_the_draft_is_the_stack_with_the_assignee_the_rules_chose(rig):
    _, repo_id, _, client = rig

    response = client.post(
        "/api/slack/draft", json={"id": f"{repo_id}:b", "assignee_id": None}
    )

    assert response.status_code == 200
    draft = response.json()
    assert draft["kind"] == "post" and draft["channel"] == "DTEST"
    assert [pr["number"] for pr in draft["prs"]] == [1, 2]
    assert draft["assignee"] == {"id": "UA", "name": "alan"}
    assert "https://github.com/o/r/pull/1" in draft["text"]
    assert "  - [Change b]" in draft["text"]  # b is indented under a


def test_a_redraft_covers_only_the_prs_named_and_lists_the_rest_unchecked(rig):
    _, repo_id, _, client = rig

    draft = client.post(
        "/api/slack/draft",
        json={
            "id": f"{repo_id}:b",
            "assignee_id": None,
            "prs": [{"repo": "o/r", "number": 2, "assignee_id": None}],
        },
    ).json()

    assert [(pr["number"], pr["included"]) for pr in draft["prs"]] == [
        (1, False),
        (2, True),
    ]
    assert "pull/1" not in draft["text"] and "pull/2" in draft["text"]
    assert draft["prs"][0]["ci"] in ("none", "running", "failed", "success")


def test_a_redraft_of_no_pr_is_a_409(rig):
    _, repo_id, _, client = rig

    response = client.post(
        "/api/slack/draft",
        json={"id": f"{repo_id}:b", "assignee_id": None, "prs": []},
    )

    assert response.status_code == 409


def test_posting_the_draft_sends_it_records_it_and_changes_the_cards(rig):
    app, repo_id, proxy, client = rig
    draft = client.post(
        "/api/slack/draft", json={"id": f"{repo_id}:a", "assignee_id": None}
    ).json()
    queue = app.state.hub.subscribe()

    response = client.post(
        "/api/slack/send",
        json={
            "id": f"{repo_id}:a",
            "kind": "post",
            "text": draft["text"],
            "assignee_id": "UA",
            "thread_ts": None,
            "prs": [
                {"repo": pr["repo"], "number": pr["number"]} for pr in draft["prs"]
            ],
        },
    )

    assert response.status_code == 202  # accepted: it posts in the background
    assert response.json()["prs"] == [
        {"repo": pr["repo"], "number": pr["number"], "assignee_id": None}
        for pr in draft["prs"]
    ]
    assert wait_until_posted(client, f"{repo_id}:a")["posted"] is True
    (call,) = proxy.sent
    assert call["channel"] == "DTEST" and call["thread_ts"] is None
    assert "<@UA>" in call["text"] and "@alan" not in call["text"].replace("<@UA>", "")
    got = branches(client)
    assert got["a"]["slack"]["posted"] is True and got["b"]["slack"]["posted"] is True
    assert got["a"]["status"]["next"]["kind"] == "request_review"
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    # each card is told when the post starts and when it is done
    states = [
        (data["branch_id"], data["slack"]["posting"], data["slack"]["posted"])
        for name, data in events
        if name == "status"
    ]
    for branch in (f"{repo_id}:a", f"{repo_id}:b"):
        assert (branch, True, False) in states and (branch, False, True) in states
        assert states.index((branch, True, False)) < states.index((branch, False, True))


def test_publish_compresses_the_branch_then_posts_the_confirmed_message(rig):
    _, repo_id, proxy, client = rig
    draft = client.post(
        "/api/slack/draft", json={"id": f"{repo_id}:b", "assignee_id": None}
    ).json()
    request = {
        "id": f"{repo_id}:b",
        "kind": "post",
        "text": draft["text"],
        "assignee_id": draft["assignee"]["id"],
        "thread_ts": None,
        "prs": [
            {"repo": pr["repo"], "number": pr["number"]}
            for pr in draft["prs"]
            if pr["included"]
        ],
    }

    response = client.post("/api/slack/publish", json=request)

    assert response.status_code == 202
    assert len(proxy.publish_calls) == 1
    assert proxy.publish_calls[0][1:] == ("b", 2, False)
    assert wait_until_posted(client, f"{repo_id}:b")["posted"] is True
    assert (
        "GitHub does not support stacked pull requests"
        in branches(client)["b"]["slack"]["notice"]
    )


def test_publish_enables_agent_merge_for_a_draft_standalone_pr_after_ready(
    draft_rig, monkeypatch
):
    app, repo_id, proxy, client = draft_rig
    calls = []

    async def put_json(path, body, *, caller):
        calls.append((path, body, caller))
        return {}

    monkeypatch.setattr(app.state.github, "put_json", put_json)
    draft = client.post(
        "/api/slack/draft", json={"id": f"{repo_id}:a", "assignee_id": None}
    ).json()
    request = {
        "id": f"{repo_id}:a",
        "kind": "post",
        "text": draft["text"],
        "assignee_id": draft["assignee"]["id"],
        "thread_ts": None,
        "prs": [{"repo": "o/r", "number": 1}],
    }

    response = client.post("/api/slack/publish", json=request)

    assert response.status_code == 202
    assert proxy.publish_calls[0][1:] == ("a", 1, True)
    assert calls == [
        (
            "agents/repos/o/r/pulls/1/agent-merge",
            {"allowed_actions": ["fix_ci", "resolve_merge_conflicts"]},
            "publish",
        )
    ]
    assert wait_until_posted(client, f"{repo_id}:a")["posted"] is True


def test_agent_merge_failure_is_reported_without_blocking_slack(rig, monkeypatch):
    app, repo_id, _proxy, client = rig

    async def put_json(path, body, *, caller):
        raise RuntimeError("Automations are disabled")

    monkeypatch.setattr(app.state.github, "put_json", put_json)
    draft = client.post(
        "/api/slack/draft", json={"id": f"{repo_id}:a", "assignee_id": None}
    ).json()
    request = {
        "id": f"{repo_id}:a",
        "kind": "post",
        "text": draft["text"],
        "assignee_id": draft["assignee"]["id"],
        "thread_ts": None,
        "prs": [{"repo": "o/r", "number": 1}],
    }

    response = client.post("/api/slack/publish", json=request)

    assert response.status_code == 202
    slack = wait_until_posted(client, f"{repo_id}:a")
    assert slack["posted"] is True
    assert "Automations are disabled" in slack["notice"]


def test_publish_marks_a_draft_ready_before_posting_it(draft_rig):
    _, repo_id, proxy, client = draft_rig
    draft = client.post(
        "/api/slack/draft", json={"id": f"{repo_id}:b", "assignee_id": None}
    ).json()
    request = {
        "id": f"{repo_id}:b",
        "kind": "post",
        "text": draft["text"],
        "assignee_id": draft["assignee"]["id"],
        "thread_ts": None,
        "prs": [
            {"repo": pr["repo"], "number": pr["number"]}
            for pr in draft["prs"]
            if pr["included"]
        ],
    }

    response = client.post("/api/slack/publish", json=request)

    assert response.status_code == 202
    assert proxy.publish_calls[0][1:] == ("b", 2, True)
    assert wait_until_posted(client, f"{repo_id}:b")["posted"] is True


def test_it_cannot_be_posted_twice(rig):
    _, repo_id, proxy, client = rig
    body = {
        "id": f"{repo_id}:a",
        "kind": "post",
        "text": "hello https://github.com/o/r/pull/1",
        "assignee_id": None,
        "thread_ts": None,
        "prs": [{"repo": "o/r", "number": 1}],
    }
    assert client.post("/api/slack/send", json=body).status_code == 202
    wait_until_posted(client, f"{repo_id}:a")

    again = client.post("/api/slack/send", json=body)

    assert again.status_code == 409 and "already posted" in again.json()["detail"]
    assert len(proxy.sent) == 1


def test_a_posted_pr_is_reassigned_in_its_thread_before_a_bump_is_due(rig):
    _, repo_id, proxy, client = rig
    reassign = {"id": f"{repo_id}:a", "assignee_id": None, "reassign": True}
    assert (
        client.post("/api/slack/draft", json=reassign).status_code == 409
    )  # not posted
    client.post(
        "/api/slack/send",
        json={
            "id": f"{repo_id}:a",
            "kind": "post",
            "text": "hello https://github.com/o/r/pull/1 @alan",
            "assignee_id": "UA",
            "thread_ts": None,
            "prs": [{"repo": "o/r", "number": 1, "assignee_id": "UA"}],
        },
    )
    wait_until_posted(client, f"{repo_id}:a")
    thread = proxy.sent[0]

    draft = client.post("/api/slack/draft", json=reassign).json()
    assert (
        branches(client)["a"]["slack"]["bump_due"] is False
    )  # a bump is not an option yet
    assert draft["kind"] == "reassign" and draft["assignee"]["name"] != "alan"
    client.post(
        "/api/slack/send",
        json={
            "id": f"{repo_id}:a",
            "kind": "reassign",
            "text": draft["text"],
            "assignee_id": draft["assignee"]["id"],
            "thread_ts": draft["thread_ts"],
            "prs": [{"repo": "o/r", "number": 1}],
        },
    )
    slack = wait_until_posted(client, f"{repo_id}:a")

    assert slack["last_kind"] == "reassign"
    assert slack["assignee"] == draft["assignee"]["name"]
    assert (
        proxy.sent[1]["thread_ts"] == draft["thread_ts"] and thread is not proxy.sent[1]
    )


def test_a_branch_with_nothing_to_post_has_no_draft(rig):
    _, repo_id, _, client = rig
    client.post(
        "/api/slack/send",
        json={
            "id": f"{repo_id}:a",
            "kind": "post",
            "text": "x https://github.com/o/r/pull/1",
            "assignee_id": None,
            "thread_ts": None,
            "prs": [{"repo": "o/r", "number": 1}],
        },
    )

    response = client.post(
        "/api/slack/draft", json={"id": f"{repo_id}:a", "assignee_id": None}
    )

    assert response.status_code == 409


def test_a_failed_send_records_nothing_and_says_why_on_the_card(rig):
    _, repo_id, proxy, client = rig
    proxy.fail = "the Slack session timed out"

    response = client.post(
        "/api/slack/send",
        json={
            "id": f"{repo_id}:a",
            "kind": "post",
            "text": "x https://github.com/o/r/pull/1",
            "assignee_id": None,
            "thread_ts": None,
            "prs": [{"repo": "o/r", "number": 1}],
        },
    )

    assert response.status_code == 202
    slack = wait_until_posted(client, f"{repo_id}:a")
    assert slack["posted"] is False
    assert "timed out" in slack["notice"]


def test_unknown_branches_and_prs_are_404(rig):
    _, repo_id, _, client = rig

    no_branch = client.post(
        "/api/slack/draft", json={"id": f"{repo_id}:nope", "assignee_id": None}
    )
    bad_id = client.post("/api/slack/draft", json={"id": "x", "assignee_id": None})
    no_pr = client.post(
        "/api/slack/send",
        json={
            "id": f"{repo_id}:a",
            "kind": "post",
            "text": "x",
            "assignee_id": None,
            "thread_ts": None,
            "prs": [{"repo": "o/r", "number": 99}],
        },
    )

    assert (no_branch.status_code, bad_id.status_code, no_pr.status_code) == (
        404,
        404,
        404,
    )


def test_without_slack_configured_the_endpoints_do_not_exist_to_use(tmp_path):
    app = create_app()
    client = TestClient(app)

    draft = client.post("/api/slack/draft", json={"id": "1:a", "assignee_id": None})
    send = client.post(
        "/api/slack/send",
        json={
            "id": "1:a",
            "kind": "post",
            "text": "x",
            "assignee_id": None,
            "thread_ts": None,
            "prs": [],
        },
    )

    assert draft.status_code == 404 and send.status_code == 404
    assert "not configured" in draft.json()["detail"]


def test_a_post_another_process_records_shows_up_on_the_card_without_a_restart(rig):
    from toolbelt.ui.slack_link import parse_message_url, rows_for_prs

    app, repo_id, _, client = rig
    assert branches(client)["a"]["slack"]["posted"] is False
    queue = app.state.hub.subscribe()

    # What ``toolbelt slack link`` does: write the posts to the database.
    store = StackStore(default_db_path())
    store.save_slack_posts(
        rows_for_prs(
            parse_message_url("https://x.slack.com/archives/DTEST/p1792696217666559"),
            [("o/r", 1)],
            ALAN,
        )
    )
    store.close()

    for _ in range(300):
        slack = branches(client)["a"]["slack"]
        if slack["posted"]:
            break
        time.sleep(0.01)
    assert slack["posted"] is True and slack["assignee"] == "alan"
    assert branches(client)["b"]["slack"]["posted"] is False  # another PR is untouched
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert any(
        name == "status"
        and data["branch_id"] == f"{repo_id}:a"
        and data["slack"]["posted"]
        for name, data in events
    )


def test_publish_puts_the_pr_on_the_merge_queue(rig):
    _, repo_id, _, client = rig
    request = {
        "id": f"{repo_id}:a",
        "kind": "post",
        "text": "please review",
        "assignee_id": "UA",
        "thread_ts": None,
        "prs": [{"repo": "o/r", "number": 1}],
    }

    response = client.post("/api/slack/publish", json=request)

    assert response.status_code == 202
    wait_until_posted(client, f"{repo_id}:a")
    queued = branches(client)["a"]["merge_queue"]
    assert queued["queued"] is True


# ---- publishing without a Slack post ----------------------------------------


def wait_until_published(client, branch_id: str) -> dict:
    """The branch's Slack info once its background publish has finished."""
    for _ in range(200):
        branch = next(
            b
            for b in client.get("/api/snapshot").json()["branches"]
            if b["id"] == branch_id
        )
        if not branch["slack"]["publishing"]:
            return branch["slack"]
        time.sleep(0.01)
    raise AssertionError("the publish did not finish")


def skip_request(repo_id: int, name: str) -> dict:
    return {"id": f"{repo_id}:{name}", "post_in_slack": False}


def test_publishing_without_a_post_publishes_posts_nothing_and_moves_the_card_on(rig):
    _, repo_id, proxy, client = rig
    events = client.app.state.hub.subscribe()  # type: ignore[attr-defined]

    response = client.post("/api/slack/publish", json=skip_request(repo_id, "a"))

    assert response.status_code == 202
    slack = wait_until_published(client, f"{repo_id}:a")
    assert proxy.publish_calls[0][1:] == ("a", 1, False)
    assert proxy.sent == []
    assert (slack["posted"], slack["skipped"], slack["publishing"]) == (
        False,
        True,
        False,
    )
    # No Post or Bump in Slack for it any more: its card moves on.
    assert branches(client)["a"]["status"]["next"]["kind"] == "request_review"
    published = []
    while not events.empty():
        published.append(events.get_nowait())
    states = [
        (data["slack"]["publishing"], data["slack"]["skipped"])
        for name, data in published
        if name == "status" and data["branch_id"] == f"{repo_id}:a"
    ]
    assert (True, True) in states and states[-1] == (False, True)
    # The other PR of the stack is still for Slack.
    assert branches(client)["b"]["status"]["next"]["kind"] == "post_slack"


def test_a_draft_published_without_a_post_is_still_offered_publish_until_it_is_ready(
    draft_rig,
):
    _, repo_id, proxy, client = draft_rig

    response = client.post("/api/slack/publish", json=skip_request(repo_id, "a"))

    assert response.status_code == 202
    wait_until_published(client, f"{repo_id}:a")
    assert proxy.publish_calls[0][1:] == ("a", 1, True)
    assert proxy.sent == []
    # The fake GitHub keeps saying draft: the publish step is still offered.
    assert branches(client)["a"]["status"]["next"]["kind"] == "publish"


def test_the_choice_not_to_post_survives_a_restart_and_the_branchs_removal(
    rig, tmp_path
):
    _, repo_id, _, client = rig
    client.post("/api/slack/publish", json=skip_request(repo_id, "a"))
    wait_until_published(client, f"{repo_id}:a")

    store = StackStore(default_db_path())
    try:
        assert store.slack_skipped_prs() == {("o/r", 1)}
        store.remove_branch(repo_id, "a")
        assert store.slack_skipped_prs() == {("o/r", 1)}
    finally:
        store.close()


def test_after_a_restart_a_skipped_pr_still_has_no_slack_step(rig):
    _, repo_id, _, client = rig
    client.post("/api/slack/publish", json=skip_request(repo_id, "a"))
    wait_until_published(client, f"{repo_id}:a")

    async def fetch(branch: str, root: Path) -> PrSnapshot:
        number = {"a": 1, "b": 2}[branch]
        return PrSnapshot(status(number), facts(number, f"Change {branch}"))

    restarted = create_app(fetch=fetch, slack_settings=CONFIG, tick=0.05)
    asyncio.run(restarted.state.refresher.run_once())
    with TestClient(restarted) as again:
        got = branches(again)

    assert got["a"]["status"]["next"]["kind"] == "request_review"
    assert got["a"]["slack"]["skipped"] is True
    assert got["b"]["status"]["next"]["kind"] == "post_slack"
    assert got["b"]["slack"]["skipped"] is False


def test_publishing_without_a_post_needs_no_slack_text_or_pr_selection_but_keeps_the_other_checks(
    rig,
):
    _, repo_id, _, client = rig

    unknown = client.post("/api/slack/publish", json=skip_request(repo_id, "nope"))
    first = client.post("/api/slack/publish", json=skip_request(repo_id, "a"))
    wait_until_published(client, f"{repo_id}:a")
    again = client.post("/api/slack/publish", json=skip_request(repo_id, "a"))

    assert unknown.status_code == 404
    assert first.status_code == 202  # no text, no PR selected
    assert again.status_code == 409 and "no longer ready" in again.json()["detail"]


def test_a_failed_publish_without_a_post_says_why_and_gives_the_slack_step_back(
    tmp_path,
):
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

    async def fetch(branch: str, root: Path) -> PrSnapshot:
        return PrSnapshot(status(1), facts(1, "Change a"))

    def failing(repo_root, branch, pr_number, *, is_draft):
        return PublishOutcome(False, "compress failed")

    app = create_app(fetch=fetch, slack_settings=CONFIG, publisher=failing, tick=0.05)
    asyncio.run(app.state.refresher.run_once())
    with TestClient(app) as client:
        client.post("/api/slack/publish", json=skip_request(repo_id, "a"))
        slack = wait_until_published(client, f"{repo_id}:a")
        got = branches(client)["a"]

    assert "compress failed" in slack["notice"]
    assert slack["skipped"] is False
    assert got["status"]["next"]["kind"] == "post_slack"


def gated_draft_app(repo_id_holder: list[int], tmp_path, *, outcome: PublishOutcome):
    """A one-branch draft app whose publish waits for ``release``."""
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
    repo_id_holder.append(repo_id)
    release = threading.Event()

    async def fetch(branch: str, root: Path) -> PrSnapshot:
        return PrSnapshot(status(1, is_draft=True), facts(1, "Change a", is_draft=True))

    def gated(repo_root, branch, pr_number, *, is_draft):
        release.wait(timeout=10)
        return outcome

    app = create_app(fetch=fetch, slack_settings=CONFIG, publisher=gated, tick=0.05)
    asyncio.run(app.state.refresher.run_once())
    return app, release


def publish_and_look_midway(client, repo_id: int, release: threading.Event) -> dict:
    """Starts a publish without a post, returns the branch while it is still
    running, then lets it finish. The test client answers a request only after
    its background work, so the request goes out on another thread."""
    request = threading.Thread(
        target=client.post,
        args=("/api/slack/publish",),
        kwargs={"json": skip_request(repo_id, "a")},
    )
    request.start()
    try:
        for _ in range(200):
            during = branches(client)["a"]
            if during["slack"]["publishing"]:
                return during
            time.sleep(0.01)
        raise AssertionError("the publish did not start")
    finally:
        release.set()
        request.join(timeout=10)


def test_a_draft_being_published_reads_publishing_and_waits_on_others(tmp_path):
    ids: list[int] = []
    app, release = gated_draft_app(ids, tmp_path, outcome=PublishOutcome(True, ""))
    repo_id = ids[0]
    with TestClient(app) as client:
        assert branches(client)["a"]["status"]["next"]["kind"] == "publish"

        during = publish_and_look_midway(client, repo_id, release)
        wait_until_published(client, f"{repo_id}:a")

    assert during["slack"]["publishing"] is True
    assert during["status"]["next"] == {
        "label": "Publishing PR…",
        "who": "others",
        "kind": "publishing",
        "would_be": None,
    }


def test_a_failed_publish_gives_the_draft_its_publish_step_back(tmp_path):
    ids: list[int] = []
    app, release = gated_draft_app(ids, tmp_path, outcome=PublishOutcome(False, "no"))
    repo_id = ids[0]
    with TestClient(app) as client:
        during = publish_and_look_midway(client, repo_id, release)
        slack = wait_until_published(client, f"{repo_id}:a")
        after = branches(client)["a"]

    assert during["status"]["next"]["kind"] == "publishing"
    assert "no" in slack["notice"]
    assert after["status"]["next"]["kind"] == "publish"
    assert after["status"]["next"]["who"] == "you"
