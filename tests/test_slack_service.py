"""Drafting and sending the Slack messages for PRs. The sender is faked:
nothing here reaches Slack."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import SlackPostRow, SlackSessionRow
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import Fetched, TrackedBranch
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_config import AssignRule, SlackConfig
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
        #: Branches an agent holds: they read "With an agent", their kind is
        #: what they would be doing otherwise.
        self.held: set[str] = set()
        self.sent: list[dict] = []
        self.read: list[dict] = []
        self.sessions: list[SlackSessionRow] = []
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
            branches=lambda: [
                TrackedBranch(1, name, Path("/r")) for name in self.parents
            ],
            fetched=lambda repo_id, name: self.looked_up.get(name),
            parents=lambda repo_id: self.parents,
            slug_of=lambda repo_id: "o/r",
            next_action=self.next_action,
            posts=lambda: list(self.rows),
            save=self.rows.extend,
            record_session=self.sessions.append,
            wall_clock=lambda: NOW,
            changed=self.changed.append,
        )

    def next_action(self, repo_id: int, name: str) -> api.NextActionInfo | None:
        kind = self.kinds.get(name)
        if kind is None:
            return None
        if name in self.held:
            return api.NextActionInfo(
                label="With an agent",
                who="agent",
                kind="claimed",
                would_be=api.WouldBeAction(label="Post in Slack", who="you", kind=kind),
            )
        return api.NextActionInfo(label=kind, who="you", kind=kind)

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
            session_id=f"send-session-{len(self.sent)}",
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


def post(rig: Rig, send_request: api.SlackSendRequest) -> api.SlackAccepted:
    """Sends and waits for the background post to finish."""

    async def go() -> api.SlackAccepted:
        accepted = await rig.service.send(send_request)
        await rig.service.idle()
        return accepted

    return asyncio.run(go())


def notice(rig: Rig, number: int) -> str | None:
    """What the PR's card says about the last post that finished."""
    info = rig.tracker.info(
        1,
        {1: "a", 2: "b", 3: "c"}[number],
        rig.looked_up[{1: "a", 2: "b", 3: "c"}[number]],
        NOW,
    )
    assert info is not None
    return info.notice


# ---- the draft of a post -----------------------------------------------------


def test_a_post_covers_the_branch_and_its_stack_bottom_first_with_one_assignee():
    rig = Rig()

    draft = rig.service.draft(1, "b", None)  # asked from the top of the stack

    assert draft.kind == "post" and draft.channel == "DTEST"
    assert [pr.number for pr in draft.prs] == [1, 2]  # not the separate branch c
    assert draft.assignee == api.SlackPerson(id="UA", name="alan")
    assert draft.text.splitlines()[0:2] == [
        "- [Bottom](https://github.com/o/r/pull/1) @alan `+100/-10`",
        "  - [Top](https://github.com/o/r/pull/2) @alan `+50/-5`",
    ]
    assert draft.thread_ts is None
    assert [c.name for c in draft.candidates] == ["will", "alan", "dan"]


def test_a_stack_member_an_agent_holds_is_listed_unchecked_and_left_out_of_the_text():
    rig = Rig()
    rig.held.add("a")

    draft = rig.service.draft(1, "b", None)

    assert [(pr.number, pr.included) for pr in draft.prs] == [(1, False), (2, True)]
    assert "Bottom" not in draft.text and "Top" in draft.text
    held = draft.prs[0]
    assert held.next is not None
    assert (held.next.label, held.next.would_be.label) == (  # type: ignore[union-attr]
        "With an agent",
        "Post in Slack",
    )


def test_a_held_member_is_not_listed_when_it_would_do_something_else():
    rig = Rig()
    rig.held.add("a")
    rig.kinds["a"] = "fix_ci"

    draft = rig.service.draft(1, "b", None)

    assert [pr.number for pr in draft.prs] == [2]


def test_each_listed_pr_says_where_it_stands():
    rig = Rig()
    rig.looked_up["a"] = Fetched(
        status=BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.FAILED,
            review=ReviewState.APPROVED,
            url="https://github.com/o/r/pull/1",
            open_comments=2,
            has_conflicts=True,
            additions=100,
            deletions=10,
        ),
        at=0.0,
        facts=rig.looked_up["a"].facts,
    )

    bottom = rig.service.draft(1, "b", None).prs[0]

    assert (bottom.pr, bottom.ci, bottom.review) == (
        PrState.OPEN,
        CiState.FAILED,
        ReviewState.APPROVED,
    )
    assert (bottom.open_comments, bottom.has_conflicts) == (2, True)


def test_a_post_covers_the_prs_asked_for_including_one_an_agent_holds():
    rig = Rig()
    rig.held.add("a")

    draft = rig.service.draft(
        1,
        "b",
        None,
        [api.SlackPrRef(repo="o/r", number=1), api.SlackPrRef(repo="o/r", number=2)],
    )

    assert [(pr.number, pr.included) for pr in draft.prs] == [(1, True), (2, True)]
    assert draft.text.splitlines()[0:2] == [
        "- [Bottom](https://github.com/o/r/pull/1) @alan `+100/-10`",
        "  - [Top](https://github.com/o/r/pull/2) @alan `+50/-5`",
    ]


def test_unchecking_a_pr_leaves_it_listed_but_out_of_the_text():
    rig = Rig()

    draft = rig.service.draft(1, "b", None, [api.SlackPrRef(repo="o/r", number=2)])

    assert [(pr.number, pr.included) for pr in draft.prs] == [(1, False), (2, True)]
    assert draft.text.splitlines()[0].startswith("- [Top]")  # no parent: top level


def test_the_person_asked_about_each_pr_of_a_redraft_is_kept():
    rig = Rig()

    draft = rig.service.draft(
        1,
        "b",
        None,
        [
            api.SlackPrRef(repo="o/r", number=1, assignee_id="UW"),
            api.SlackPrRef(repo="o/r", number=2, assignee_id="UD"),
        ],
    )

    assert [pr.assignee.name for pr in draft.prs] == ["will", "dan"]  # type: ignore[union-attr]
    assert "@will" in draft.text.splitlines()[0]
    assert "@dan" in draft.text.splitlines()[1]


def test_a_post_of_no_pr_is_refused():
    rig = Rig()

    with pytest.raises(SlackNotApplicable, match="at least one"):
        rig.service.draft(1, "b", None, [])


def test_a_post_leaves_out_stack_members_that_are_not_ready():
    rig = Rig()
    rig.kinds["a"] = "fix_ci"

    draft = rig.service.draft(1, "b", None)

    assert [pr.number for pr in draft.prs] == [2]
    assert draft.text.splitlines()[0].startswith(
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

    accepted = post(rig, request(numbers=(1, 2)))

    (call,) = rig.sent
    assert call["channel"] == "DTEST"
    assert call["thread_ts"] is None
    assert call["text"] == (
        "hello <@UA> https://github.com/o/r/pull/1 https://github.com/o/r/pull/2"
    )
    assert accepted.prs == request(numbers=(1, 2)).prs
    assert notice(rig, 1) is None and notice(rig, 2) is None  # verified
    assert {(r.repo, r.pr_number, r.kind, r.assignee_name) for r in rig.rows} == {
        ("o/r", 1, "post", "alan"),
        ("o/r", 2, "post", "alan"),
    }
    assert {r.channel for r in rig.rows} == {"DTEST"}
    assert all(r.posted_at == NOW for r in rig.rows)
    # told once when it starts (the PRs read posting) and once when it is done
    assert rig.changed == [{(1, "a"), (1, "b")}, {(1, "a"), (1, "b")}]


def test_after_sending_the_tracker_sees_the_pr_as_posted():
    rig = Rig()

    post(rig, request())

    info = rig.tracker.info(1, "a", rig.looked_up["a"], NOW)
    assert info is not None and info.posted and info.last_kind == "post"


def test_the_channel_is_never_taken_from_the_request():
    rig = Rig()
    forged = api.SlackSendRequest.model_validate(
        {**request().model_dump(), "id": "1:a"}
    )

    post(rig, forged)

    assert rig.sent[0]["channel"] == "DTEST"
    assert "channel" not in api.SlackSendRequest.model_fields


def test_a_pr_already_posted_is_not_posted_again():
    rig = Rig()
    rig.post_row(1, 2)

    with pytest.raises(SlackNotApplicable, match="already posted"):
        post(rig, request())
    assert rig.sent == []


def test_a_bump_is_sent_only_as_a_reply_in_the_thread_the_pr_was_posted_in():
    rig = Rig()
    rig.post_row(1, 30, thread="1.0")

    post(rig, request("bump", thread_ts="1.0"))

    assert rig.sent[0]["thread_ts"] == "1.0"
    assert [r.kind for r in rig.rows if r.ts != "30.1"] == ["bump"]
    assert rig.rows[-1].thread_ts == "1.0"


def test_a_bump_with_the_wrong_thread_or_for_a_pr_never_posted_is_refused():
    rig = Rig()
    rig.post_row(1, 30, thread="1.0")

    with pytest.raises(SlackNotApplicable, match="not posted in that thread"):
        post(rig, request("bump", thread_ts="2.0"))
    with pytest.raises(SlackNotApplicable, match="not posted in that thread"):
        post(rig, request("bump", numbers=(3,), thread_ts="1.0"))
    assert rig.sent == []


def test_a_pr_that_is_not_tracked_is_refused():
    rig = Rig()

    with pytest.raises(SlackUnknown):
        post(rig, request(numbers=(99,)))
    with pytest.raises(SlackNotApplicable, match="names no PR"):
        post(rig, request(numbers=()))


def test_a_message_missing_a_link_in_slacks_copy_is_posted_with_a_warning():
    rig = Rig()
    rig.read_back = "something else entirely"

    post(rig, request())

    warning = notice(rig, 1)
    assert warning is not None and "https://github.com/o/r/pull/1" in warning
    assert len(rig.rows) == 1  # it was posted, so it is recorded


def test_a_message_that_could_not_be_read_back_is_posted_with_a_warning():
    rig = Rig()
    rig.read_back = None

    post(rig, request())

    warning = notice(rig, 1)
    assert warning is not None and "could not be read back" in warning


def test_a_failed_send_records_nothing():
    rig = Rig()

    async def failing(**kwargs):
        raise RuntimeError("Slack refused")

    rig.service._send = failing

    post(rig, request())  # accepted: the failure comes later, on the PR

    assert rig.rows == []
    assert notice(rig, 1) == "Posting in Slack failed: Slack refused"
    info = rig.tracker.info(1, "a", rig.looked_up["a"], NOW)
    assert info is not None and not info.posted and not info.posting
    assert rig.changed == [{(1, "a")}, {(1, "a")}]  # posting began, then ended


def test_while_a_post_is_in_flight_the_pr_reads_posting_and_cannot_be_posted_again():
    rig = Rig()
    release = asyncio.Event()
    gate = rig.send_message

    async def slow(**kwargs):
        await release.wait()
        return await gate(**kwargs)

    rig.service._send = slow

    async def go():
        await rig.service.send(request())
        during = rig.tracker.info(1, "a", rig.looked_up["a"], NOW)
        with pytest.raises(SlackNotApplicable, match="already being posted"):
            await rig.service.send(request())
        release.set()
        await rig.service.idle()
        return during

    during = asyncio.run(go())

    assert during is not None and during.posting and not during.posted
    after = rig.tracker.info(1, "a", rig.looked_up["a"], NOW)
    assert after is not None and after.posted and not after.posting
    assert len(rig.sent) == 1


def test_a_new_post_clears_the_notice_of_the_one_before():
    rig = Rig()
    rig.read_back = None
    post(rig, request())
    assert notice(rig, 1) is not None
    rig.rows.clear()  # forget the post, as a failed one would have
    rig.tracker.reload()
    rig.read_back = "OK"

    post(rig, request())

    assert notice(rig, 1) is None


def test_the_person_asked_is_recorded_only_when_they_are_a_configured_reviewer():
    rig = Rig()

    post(rig, request(assignee_id="UNKNOWN"))

    assert rig.rows[0].assignee_id is None and rig.rows[0].assignee_name is None


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
        post(rig, request("bump", thread_ts="1.0"))

    assert rig.sent == []


# ---- what the sessions cost --------------------------------------------------


def test_a_post_records_the_session_that_sent_it_with_its_cost():
    rig = Rig()

    post(rig, request())

    assert rig.sessions == [SlackSessionRow("send-session-1", NOW, "post", 0.05)]


def test_a_bump_records_its_session_as_a_bump():
    rig = Rig()
    rig.post_row(1, hours_ago=30, thread="1.0")

    post(rig, request("bump", thread_ts="1.0"))

    assert [s.purpose for s in rig.sessions] == ["bump"]


def test_a_session_with_no_id_is_not_recorded():
    rig = Rig()

    async def anonymous(**kwargs):
        return SentMessage(
            link="https://s.slack.com/archives/DTEST/p1792600000000100",
            ts="1792600000.000100",
            thread_ts="1792600000.000100",
            read_back="x",
            cost_usd=0.05,
        )

    rig.service._send = anonymous

    post(rig, request())

    assert rig.sessions == []


# ---- the link to the original post -------------------------------------------


def test_a_bump_links_to_the_post_it_replies_to():
    rig = Rig()
    rig.kinds["a"] = "bump_slack"
    rig.post_row(1, hours_ago=30, thread="1792500000.000100")
    rig.service._config = SlackConfig(
        channel="DTEST", reviewers=(WILL, ALAN, DAN), workspace="acme"
    )

    draft = rig.service.draft(1, "a", None)

    assert (
        draft.thread_link == "https://acme.slack.com/archives/DTEST/p1792500000000100"
    )


def test_a_bump_has_no_link_without_a_workspace_and_a_post_never_has_one():
    rig = Rig()
    rig.kinds["a"] = "bump_slack"
    rig.post_row(1, hours_ago=30, thread="1792500000.000100")

    assert rig.service.draft(1, "a", None).thread_link is None  # no workspace set
    assert rig.service.draft(1, "c", None).thread_link is None  # c is a post


# ---- asking a different person about each PR ---------------------------------


def test_every_pr_of_a_draft_carries_who_it_asks():
    rig = Rig()

    draft = rig.service.draft(1, "b", None)

    assert [pr.assignee for pr in draft.prs] == [
        api.SlackPerson(id="UA", name="alan")
    ] * 2


def with_assignees(send_request: api.SlackSendRequest, *ids: str | None):
    """The request with each of its PRs asking the person ``ids`` names."""
    return send_request.model_copy(
        update={
            "prs": [
                ref.model_copy(update={"assignee_id": person})
                for ref, person in zip(send_request.prs, ids, strict=True)
            ]
        }
    )


def test_a_post_of_two_prs_can_ask_a_different_person_about_each():
    rig = Rig()

    post(rig, with_assignees(request(numbers=(1, 2)), "UW", "UD"))

    assert {(r.pr_number, r.assignee_name) for r in rig.rows} == {
        (1, "will"),
        (2, "dan"),
    }


def test_a_pr_with_no_assignee_of_its_own_is_asked_the_messages_assignee():
    rig = Rig()

    post(rig, with_assignees(request(numbers=(1, 2), assignee_id="UD"), "UW", None))

    assert {(r.pr_number, r.assignee_name) for r in rig.rows} == {
        (1, "will"),
        (2, "dan"),
    }
