"""Linking a Slack message posted by hand to its PRs: reading the message link
and the PR links, the posts recorded, and what is read from Slack. Slack itself
is a fake reader."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from toolbelt.git.stack.store import StackStore
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_config import SlackConfig
from toolbelt.ui.slack_import import ChannelMessage, MessageRead
from toolbelt.ui.slack_link import (
    LinkError,
    MessageRef,
    link_message,
    parse_message_url,
    parse_pr,
    rows_for_prs,
)

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
WILL, ALAN = Person("will.james", "UW"), Person("alan", "UA")
CONFIG = SlackConfig(channel="CREVIEW", reviewers=(WILL, ALAN))
URL = "https://dbt-labs.slack.com/archives/C0A0TFP0XC6/p1792696217666559"


class Reader:
    def __init__(
        self, text: str, *, thread_ts: str | None = None, session="s1"
    ) -> None:
        self.text = text
        self.thread_ts = thread_ts
        self.session = session
        self.asked: list[dict] = []

    async def read_message(self, **kwargs) -> MessageRead:
        self.asked.append(kwargs)
        return MessageRead(
            ChannelMessage(kwargs["ts"], self.thread_ts, self.text), 0.02, self.session
        )


@pytest.fixture
def store(tmp_path: Path) -> StackStore:
    opened = StackStore(tmp_path / "stacks.db")
    yield opened
    opened.close()


# ---- the message link -------------------------------------------------------------


def test_a_message_link_gives_its_channel_and_timestamp():
    assert parse_message_url(URL) == MessageRef(
        "C0A0TFP0XC6", "1792696217.666559", None
    )


def test_a_link_to_a_reply_names_the_thread_it_is_in():
    ref = parse_message_url(URL + "?thread_ts=1792600000.000100&cid=C0A0TFP0XC6")

    assert ref.thread_ts == "1792600000.000100"


def test_a_thread_link_to_the_threads_own_first_message_is_not_a_reply():
    ref = parse_message_url(URL + "?thread_ts=1792696217.666559&cid=C0A0TFP0XC6")

    assert ref.thread_ts is None


def test_a_direct_message_link_is_read_too():
    assert parse_message_url(
        "https://dbt-labs.slack.com/archives/D0CB4R31S73/p1792688225039229"
    ) == MessageRef("D0CB4R31S73", "1792688225.039229", None)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "https://example.com/",
        "https://x.slack.com/archives/C1",
        "C0A0TFP0XC6/p1792696217666559x",
    ],
)
def test_something_that_is_not_a_message_link_is_refused(text):
    with pytest.raises(LinkError, match="not a link to a Slack message"):
        parse_message_url(text)


def test_a_pr_link_gives_its_repo_in_lower_case_and_its_number():
    assert parse_pr("https://github.com/DBT-Labs/ai-codegen-api/pull/1750/files") == (
        "dbt-labs/ai-codegen-api",
        1750,
    )


def test_something_that_is_not_a_pr_link_is_refused():
    with pytest.raises(LinkError, match="not a link to a GitHub PR"):
        parse_pr("https://github.com/o/r/issues/4")


# ---- the posts ---------------------------------------------------------------------


def test_the_message_is_the_post_of_each_pr_with_its_time_and_who_was_asked():
    ref = parse_message_url(URL)

    rows = rows_for_prs(ref, [("o/r", 1), ("o/r", 2)], WILL)

    assert [(r.repo, r.pr_number, r.kind, r.assignee_name) for r in rows] == [
        ("o/r", 1, "post", "will.james"),
        ("o/r", 2, "post", "will.james"),
    ]
    assert {(r.channel, r.ts, r.thread_ts) for r in rows} == {
        ("C0A0TFP0XC6", "1792696217.666559", "1792696217.666559")
    }
    assert rows[0].posted_at == datetime.fromtimestamp(1792696217.666559, UTC)


def test_a_reply_is_a_bump_in_its_thread():
    ref = parse_message_url(URL + "?thread_ts=1792600000.000100")

    (row,) = rows_for_prs(ref, [("o/r", 1)], None)

    assert (row.kind, row.thread_ts, row.assignee_name) == (
        "bump",
        "1792600000.000100",
        None,
    )


# ---- linking -----------------------------------------------------------------------


def link(store, reader, **kwargs):
    arguments = {"message_url": URL, "pr_urls": [], "ask": None, "now": NOW} | kwargs
    return link_message(store, CONFIG, reader, **arguments)


def test_given_prs_are_linked_without_reading_slack(store):
    reader = Reader("must not be read")

    rows = link(store, reader, pr_urls=["https://github.com/o/r/pull/7"], ask="alan")

    assert reader.asked == []
    assert [(r.repo, r.pr_number, r.assignee_name) for r in rows] == [
        ("o/r", 7, "alan")
    ]
    assert [(r.pr_number, r.assignee_id) for r in store.slack_posts()] == [(7, "UA")]


def test_without_prs_they_are_read_out_of_the_message(store):
    reader = Reader(
        "- <https://github.com/o/r/pull/1|One> <@UW|will.james> `+1/-1`\n"
        "- <https://github.com/o/r/pull/2|Two> <@UA|alan> `+2/-2`"
    )

    rows = link(store, reader)

    assert reader.asked == [
        {"channel": "C0A0TFP0XC6", "ts": "1792696217.666559", "thread_ts": None}
    ]
    assert [(r.pr_number, r.assignee_name) for r in rows] == [
        (1, "will.james"),
        (2, "alan"),
    ]
    assert len(store.slack_posts()) == 2


def test_the_session_that_read_the_message_is_recorded_in_the_spend(store):
    link(store, Reader("https://github.com/o/r/pull/1"))

    (session,) = store.slack_sessions(since=datetime(2026, 1, 1, tzinfo=UTC))
    assert (session.session_id, session.purpose, session.cost_usd) == (
        "s1",
        "import",
        0.02,
    )


def test_who_was_asked_can_be_named_over_what_the_message_mentions(store):
    rows = link(
        store, Reader("https://github.com/o/r/pull/1 <@UW|will.james>"), ask="alan"
    )

    assert [r.assignee_name for r in rows] == ["alan"]


def test_a_reply_read_from_slack_is_a_bump_in_its_thread(store):
    reader = Reader("https://github.com/o/r/pull/1", thread_ts="1792600000.000100")

    rows = link(store, reader, message_url=URL + "?thread_ts=1792600000.000100")

    assert reader.asked[0]["thread_ts"] == "1792600000.000100"
    assert [(r.kind, r.thread_ts) for r in rows] == [("bump", "1792600000.000100")]


def test_a_message_that_links_no_pr_asks_for_them_to_be_named(store):
    with pytest.raises(LinkError, match="--pr"):
        link(store, Reader("thanks all!"))

    assert store.slack_posts() == []


def test_a_person_who_is_not_a_reviewer_is_refused(store):
    with pytest.raises(LinkError, match="not one of the slack.reviewers"):
        link(
            store,
            Reader("x"),
            pr_urls=["https://github.com/o/r/pull/1"],
            ask="nobody.here",
        )

    assert store.slack_posts() == []


def test_linking_twice_records_each_post_once(store):
    reader = Reader("https://github.com/o/r/pull/1")

    link(store, reader)
    link(store, reader)

    assert len(store.slack_posts()) == 1


def test_the_reader_is_not_asked_when_the_link_is_bad(store):
    reader = Reader("x")

    with pytest.raises(LinkError):
        link(store, reader, message_url="https://example.com/nope")

    assert reader.asked == []
