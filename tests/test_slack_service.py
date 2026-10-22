"""Drafting and sending the Slack messages for PRs. The sender is faked:
nothing here reaches Slack."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import SlackPostRow
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import Fetched, TrackedBranch
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.slack_compose import ATTRIBUTION, Person
from toolbelt.ui.slack_config import AssignRule, SlackConfig
from toolbelt.ui.slack_import import ChannelMessage
from toolbelt.ui.slack_proxy import SentMessage
from toolbelt.ui.slack_service import (
    SlackNotApplicable,
    SlackService,
    SlackUnknown,
)
from toolbelt.ui.slack_tracker import SlackTracker

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
WILL, ALAN, DAN = Person("will", "UW"), Person("alan", "UA"), Person("dan", "UD")
CONFIG = SlackConfig(
    channel="DTEST",
    reviewers=(WILL, ALAN, DAN),
    rules=(AssignRule(to="alan"),),
    user_id="UDEV",
)


def fetched(number: int, title: str, lines=(10, 2)) -> Fetched:
    status = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.REQUESTED,
        url=f"https://github.com/o/r/pull/{number}",
    )
    facts = PrFacts(
        number=number,
        title=title,
        author="devon",
        base_ref="main",
        head_oid="abc",
        state="OPEN",
        is_draft=False,
        created_at=NOW - timedelta(days=3),
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=(),
        additions=lines[0],
        deletions=lines[1],
    )
    return Fetched(status=status, at=0.0, facts=facts)


class Rig:
    """Branches a (bottom of a stack, PR 1), b (on a, PR 2) and c (separate, PR 3)."""

    def __init__(self) -> None:
        self.rows: list[SlackPostRow] = []
        self.parents = {"a": "main", "b": "a", "c": "main"}
        self.looked_up = {
            "a": fetched(1, "DI-1: Bottom", (100, 10)),
            "b": fetched(2, "DI-2: Top", (50, 5)),
            "c": fetched(3, "DI-3: Alone"),
        }
        self.kinds = {"a": "post_slack", "b": "post_slack", "c": "post_slack"}
        self.sent: list[dict] = []
        self.read: list[dict] = []
        self.messages: list[ChannelMessage] = []
        self.changed: list[set] = []
        self.read_back: str | None = "OK"
        self.tracker = SlackTracker(
            posts=lambda: list(self.rows),
            slug_of=lambda repo_id: "o/r",
            bump_after=timedelta(hours=24),
        )
        self.service = SlackService(
            config=CONFIG,
            tracker=self.tracker,
            send_message=self.send_message,
            read_messages=self.read_messages,
            branches=lambda: [
                TrackedBranch(1, name, Path("/r")) for name in self.parents
            ],
            fetched=lambda repo_id, name: self.looked_up.get(name),
            parents=lambda repo_id: self.parents,
            slug_of=lambda repo_id: "o/r",
            next_kind=lambda repo_id, name: self.kinds.get(name),
            posts=lambda: list(self.rows),
            save=self.rows.extend,
            wall_clock=lambda: NOW,
            changed=self.changed.append,
        )

    async def read_messages(self, **kwargs):
        self.read.append(kwargs)
        return self.messages, 0.06

    async def send_message(self, **kwargs) -> SentMessage:
        self.sent.append(kwargs)
        ts = f"17926000{len(self.sent):02d}.000100"
        read_back = self.read_back if self.read_back != "OK" else kwargs["text"]
        return SentMessage(
            link=f"https://s.slack.com/archives/DTEST/p{ts.replace('.', '')}",
            ts=ts,
            thread_ts=kwargs.get("thread_ts") or ts,
            read_back=read_back,
            cost_usd=0.05,
        )

    def post_row(
        self, number: int, hours_ago: float, kind="post", thread="1.0", assignee="will"
    ):
        row = SlackPostRow(
            channel="DTEST",
            ts=f"{hours_ago}.{number}",
            thread_ts=thread,
            repo="o/r",
            pr_number=number,
            kind=kind,
            posted_at=NOW - timedelta(hours=hours_ago),
            assignee_id={"will": "UW", "alan": "UA", "dan": "UD"}[assignee],
            assignee_name=assignee,
        )
        self.rows.append(row)
        self.tracker.reload()


def request(kind="post", text=None, numbers=(1,), **overrides) -> api.SlackSendRequest:
    if text is None:
        links = " ".join(f"https://github.com/o/r/pull/{n}" for n in numbers)
        text = f"hello @alan {links}".strip()
    base = {
        "id": "1:a",
        "kind": kind,
        "text": text,
        "assignee_id": "UA",
        "thread_ts": None,
        "prs": [api.SlackPrRef(repo="o/r", number=n) for n in numbers],
    }
    base.update(overrides)
    return api.SlackSendRequest(**base)  # type: ignore[arg-type]


# ---- the draft of a post -----------------------------------------------------


def test_a_post_covers_the_branch_and_its_stack_bottom_first_with_one_assignee():
    rig = Rig()

    draft = rig.service.draft(1, "b", None)  # asked from the top of the stack

    assert draft.kind == "post" and draft.channel == "DTEST"
    assert [pr.number for pr in draft.prs] == [1, 2]  # not the separate branch c
    assert draft.assignee == api.SlackPerson(id="UA", name="alan")
    assert draft.text.splitlines()[1:3] == [
        "- [Bottom](https://github.com/o/r/pull/1) @alan `+100/-10`",
        "  - [Top](https://github.com/o/r/pull/2) @alan `+50/-5`",
    ]
    assert draft.thread_ts is None
    assert [c.name for c in draft.candidates] == ["will", "alan", "dan"]


def test_a_post_leaves_out_stack_members_that_are_not_ready():
    rig = Rig()
    rig.kinds["a"] = "fix_ci"

    draft = rig.service.draft(1, "b", None)

    assert [pr.number for pr in draft.prs] == [2]
    assert draft.text.splitlines()[1].startswith(
        "- [Top]"
    )  # no parent in the post: top level


def test_the_assignee_can_be_chosen_instead_of_the_rules():
    rig = Rig()

    draft = rig.service.draft(1, "c", "UD")

    assert draft.assignee == api.SlackPerson(id="UD", name="dan")
    assert "@dan" in draft.text


def test_a_branch_whose_next_action_is_not_about_slack_has_no_draft():
    rig = Rig()
    rig.kinds["c"] = "wait_review"

    with pytest.raises(SlackNotApplicable):
        rig.service.draft(1, "c", None)


# ---- the draft of a bump -----------------------------------------------------


def test_a_bump_replies_in_the_thread_and_asks_someone_other_than_who_was_asked():
    rig = Rig()
    rig.post_row(3, 30, thread="1.0", assignee="alan")
    rig.kinds["c"] = "bump_slack"

    draft = rig.service.draft(1, "c", None)

    assert draft.kind == "bump" and draft.thread_ts == "1.0"
    assert draft.assignee is not None and draft.assignee.name != "alan"
    assert "has no comments or reviews yet" in draft.text
    assert "https://github.com/o/r/pull/3" in draft.text
    assert [pr.number for pr in draft.prs] == [3]


def test_a_bump_names_every_pr_of_the_thread_that_is_due_and_only_those():
    rig = Rig()
    rig.post_row(1, 30, thread="1.0")
    rig.post_row(2, 30, thread="1.0")
    rig.post_row(3, 30, thread="9.0")  # another thread
    rig.kinds["a"] = "bump_slack"

    draft = rig.service.draft(1, "a", None)

    assert [pr.number for pr in draft.prs] == [1, 2]


# ---- sending -------------------------------------------------------------------


def test_sending_posts_the_text_to_the_configured_channel_with_mentions_and_records_it():
    rig = Rig()

    sent = asyncio.run(rig.service.send(request(numbers=(1, 2))))

    (call,) = rig.sent
    assert call["channel"] == "DTEST"
    assert call["thread_ts"] is None
    assert call["text"] == (
        f"{ATTRIBUTION}\nhello <@UA> https://github.com/o/r/pull/1 https://github.com/o/r/pull/2"
    )
    assert sent.verified and sent.warning is None and sent.cost_usd == 0.05
    assert {(r.repo, r.pr_number, r.kind, r.assignee_name) for r in rig.rows} == {
        ("o/r", 1, "post", "alan"),
        ("o/r", 2, "post", "alan"),
    }
    assert {r.channel for r in rig.rows} == {"DTEST"}
    assert all(r.posted_at == NOW for r in rig.rows)
    assert rig.changed == [{(1, "a"), (1, "b")}]


def test_after_sending_the_tracker_sees_the_pr_as_posted():
    rig = Rig()

    asyncio.run(rig.service.send(request()))

    info = rig.tracker.info(1, "a", rig.looked_up["a"], NOW)
    assert info is not None and info.posted and info.last_kind == "post"


def test_the_channel_is_never_taken_from_the_request():
    rig = Rig()
    forged = api.SlackSendRequest.model_validate(
        {**request().model_dump(), "id": "1:a"}
    )

    asyncio.run(rig.service.send(forged))

    assert rig.sent[0]["channel"] == "DTEST"
    assert "channel" not in api.SlackSendRequest.model_fields


def test_a_pr_already_posted_is_not_posted_again():
    rig = Rig()
    rig.post_row(1, 2)

    with pytest.raises(SlackNotApplicable, match="already posted"):
        asyncio.run(rig.service.send(request()))
    assert rig.sent == []


def test_a_bump_is_sent_only_as_a_reply_in_the_thread_the_pr_was_posted_in():
    rig = Rig()
    rig.post_row(1, 30, thread="1.0")

    asyncio.run(rig.service.send(request("bump", thread_ts="1.0")))

    assert rig.sent[0]["thread_ts"] == "1.0"
    assert [r.kind for r in rig.rows if r.ts != "30.1"] == ["bump"]
    assert rig.rows[-1].thread_ts == "1.0"


def test_a_bump_with_the_wrong_thread_or_for_a_pr_never_posted_is_refused():
    rig = Rig()
    rig.post_row(1, 30, thread="1.0")

    with pytest.raises(SlackNotApplicable, match="not posted in that thread"):
        asyncio.run(rig.service.send(request("bump", thread_ts="2.0")))
    with pytest.raises(SlackNotApplicable, match="not posted in that thread"):
        asyncio.run(rig.service.send(request("bump", numbers=(3,), thread_ts="1.0")))
    assert rig.sent == []


def test_a_pr_that_is_not_tracked_is_refused():
    rig = Rig()

    with pytest.raises(SlackUnknown):
        asyncio.run(rig.service.send(request(numbers=(99,))))
    with pytest.raises(SlackNotApplicable, match="names no PR"):
        asyncio.run(rig.service.send(request(numbers=())))


def test_a_message_missing_a_link_in_slacks_copy_is_posted_with_a_warning():
    rig = Rig()
    rig.read_back = "something else entirely"

    sent = asyncio.run(rig.service.send(request()))

    assert not sent.verified
    assert sent.warning is not None and "https://github.com/o/r/pull/1" in sent.warning
    assert len(rig.rows) == 1  # it was posted, so it is recorded


def test_a_message_that_could_not_be_read_back_is_posted_with_a_warning():
    rig = Rig()
    rig.read_back = None

    sent = asyncio.run(rig.service.send(request()))

    assert not sent.verified and sent.warning is not None
    assert "could not be read back" in sent.warning


def test_a_failed_send_records_nothing():
    rig = Rig()

    async def failing(**kwargs):
        raise RuntimeError("Slack refused")

    rig.service._send = failing

    with pytest.raises(RuntimeError):
        asyncio.run(rig.service.send(request()))

    assert rig.rows == [] and rig.changed == []


def test_the_person_asked_is_recorded_only_when_they_are_a_configured_reviewer():
    rig = Rig()

    asyncio.run(rig.service.send(request(assignee_id="UNKNOWN")))

    assert rig.rows[0].assignee_id is None and rig.rows[0].assignee_name is None


# ---- importing what was posted by hand ---------------------------------------


def test_an_import_records_the_users_own_posts_so_their_prs_read_as_posted():
    rig = Rig()
    rig.messages = [
        ChannelMessage(
            "1792000000.000100",
            None,
            "- <https://github.com/o/r/pull/1|One> <@UA|alan> `+1/-1`",
        )
    ]

    imported = asyncio.run(rig.service.import_recent(days=5))

    assert imported == api.SlackImported(messages=1, prs=1, cost_usd=0.06)
    assert [(r.channel, r.pr_number, r.kind) for r in rig.rows] == [
        ("DTEST", 1, "post")
    ]
    assert rig.tracker.info(1, "a", rig.looked_up["a"], NOW).posted
    assert rig.changed == [{(1, "a")}]


def test_an_import_reads_the_users_messages_of_the_last_days_in_the_configured_channel():
    rig = Rig()

    asyncio.run(rig.service.import_recent(days=5))

    assert rig.read == [
        {"channel": "DTEST", "user_id": "UDEV", "since": NOW - timedelta(days=5)}
    ]


def test_an_import_can_read_another_channel_and_records_posts_under_it():
    rig = Rig()
    rig.messages = [
        ChannelMessage("1792000000.000100", None, "https://github.com/o/r/pull/3")
    ]

    asyncio.run(rig.service.import_recent(days=5, channel="CREVIEW"))

    assert rig.read[0]["channel"] == "CREVIEW"
    assert [r.channel for r in rig.rows] == ["CREVIEW"]


def test_an_import_needs_to_know_whose_posts_to_read():
    rig = Rig()
    rig.service._config = SlackConfig(channel="DTEST")

    with pytest.raises(SlackNotApplicable, match="user_id"):
        asyncio.run(rig.service.import_recent(days=5))

    assert rig.read == []


def test_importing_twice_records_each_post_once():
    rig = Rig()
    rig.messages = [
        ChannelMessage("1792000000.000100", None, "https://github.com/o/r/pull/1")
    ]
    saved: dict = {}
    rig.service._save = lambda rows: saved.update(
        {(r.channel, r.ts, r.repo, r.pr_number): r for r in rows}
    )

    asyncio.run(rig.service.import_recent(days=5))
    asyncio.run(rig.service.import_recent(days=5))

    assert len(saved) == 1


# ---- a thread in another channel is never replied to -------------------------


def test_a_post_found_in_another_channel_is_not_bumped():
    rig = Rig()
    rig.kinds["a"] = "bump_slack"
    rig.post_row(1, hours_ago=30)
    rig.rows[0] = SlackPostRow(**{**rig.rows[0].__dict__, "channel": "CREVIEW"})
    rig.tracker.reload()

    with pytest.raises(SlackNotApplicable, match="CREVIEW"):
        rig.service.draft(1, "a", None)
    with pytest.raises(SlackNotApplicable, match="CREVIEW"):
        asyncio.run(rig.service.send(request("bump", thread_ts="1.0")))

    assert rig.sent == []
