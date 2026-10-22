"""`/api/slack/draft` and `/api/slack/send` through the app: a branch's draft,
posting it, the recording and the events that follow. The Slack sender is
faked: nothing here reaches Slack."""

import asyncio
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
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_config import AssignRule, SlackConfig
from toolbelt.ui.slack_import import ChannelMessage, ChannelRead
from toolbelt.ui.slack_proxy import SentMessage, SlackProxyError

ALAN, WILL = Person("alan", "UA"), Person("will", "UW")
CONFIG = SlackConfig(
    channel="DTEST",
    reviewers=(WILL, ALAN),
    rules=(AssignRule(to="alan"),),
    user_id="UDEV",
)


class FakeProxy:
    def __init__(self) -> None:
        self.sent: list[dict] = []
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


class FakeReader:
    def __init__(self) -> None:
        self.read: list[dict] = []
        self.messages: list[ChannelMessage] = []
        self.fail: str | None = None

    async def own_messages(self, **kwargs):
        if self.fail:
            raise SlackProxyError(self.fail)
        self.read.append(kwargs)
        return ChannelRead(self.messages, 0.06, "read-session")


def facts(number: int, title: str) -> PrFacts:
    return PrFacts(
        number=number,
        title=title,
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


def status(number: int) -> BranchStatus:
    return BranchStatus(
        pr=PrState.OPEN,
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
    reader = FakeReader()
    app = create_app(
        fetch=fetch,
        slack_settings=CONFIG,
        slack_proxy=proxy,  # type: ignore[arg-type]
        slack_reader=reader,  # type: ignore[arg-type]
        tick=0.05,  # a change another process makes to the database is seen at once
    )
    asyncio.run(app.state.refresher.run_once())
    app.state.reader = reader
    # In a ``with`` so the app's event loop outlives a request: a post runs in
    # the background after its request has been answered.
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


def test_an_import_marks_the_prs_the_user_already_posted_as_posted(rig):
    app, _, proxy, client = rig
    app.state.reader.messages = [
        ChannelMessage(
            "1792000000.000100",
            None,
            "- <https://github.com/o/r/pull/1|One> <@UA|alan>",
        )
    ]

    response = client.post("/api/slack/import", json={"days": 5, "channel": None})

    assert response.status_code == 200
    assert response.json() == {"messages": 1, "prs": 1, "cost_usd": 0.06}
    got = branches(client)
    assert got["a"]["slack"]["posted"] is True
    assert got["b"]["slack"]["posted"] is False
    assert proxy.sent == []  # reading never posts


def test_a_failed_import_is_a_bad_gateway(rig):
    app, _, _, client = rig
    app.state.reader.fail = "the session timed out"

    response = client.post("/api/slack/import", json={"days": 5, "channel": None})

    assert response.status_code == 502


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
