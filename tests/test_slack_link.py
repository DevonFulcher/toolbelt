"""Linking a Slack message posted by hand to its PRs: reading the message link
and the PR links, the posts recorded, and what is read from Slack. Slack itself
is a fake reader."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from toolbelt.git.stack.store import StackStore
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_config import SlackConfig
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

PR_7 = "https://github.com/o/r/pull/7"


def link(store, **kwargs):
    arguments = {"message_url": URL, "pr_urls": [PR_7], "ask": None} | kwargs
    return link_message(store, CONFIG, **arguments)


def test_the_given_prs_are_recorded_with_who_was_asked(store):
    rows = link(store, pr_urls=[PR_7, "https://github.com/o/r/pull/8"], ask="alan")

    assert [(r.repo, r.pr_number, r.assignee_name) for r in rows] == [
        ("o/r", 7, "alan"),
        ("o/r", 8, "alan"),
    ]
    assert [(r.pr_number, r.assignee_id) for r in store.slack_posts()] == [
        (7, "UA"),
        (8, "UA"),
    ]


def test_nobody_is_asked_unless_named(store):
    (row,) = link(store)

    assert (row.assignee_id, row.assignee_name) == (None, None)


def test_a_reply_is_recorded_as_a_bump_in_its_thread(store):
    rows = link(store, message_url=URL + "?thread_ts=1792600000.000100")

    assert [(r.kind, r.thread_ts) for r in rows] == [("bump", "1792600000.000100")]


def test_the_prs_must_be_named_because_nothing_is_read_from_slack(store):
    with pytest.raises(LinkError, match="--pr"):
        link(store, pr_urls=[])

    assert store.slack_posts() == []


def test_a_reviewer_is_recorded_with_their_slack_id(store):
    (row,) = link(store, ask="will.james")

    assert (row.assignee_name, row.assignee_id) == ("will.james", "UW")


def test_anyone_else_asked_is_recorded_by_the_name_given_without_an_id(store):
    (row,) = link(store, ask="matthew.ingle")

    assert (row.assignee_name, row.assignee_id) == ("matthew.ingle", None)
    (stored,) = store.slack_posts()
    assert (stored.assignee_name, stored.assignee_id) == ("matthew.ingle", None)


def test_a_bad_message_link_or_pr_link_records_nothing(store):
    with pytest.raises(LinkError):
        link(store, message_url="https://example.com/nope")
    with pytest.raises(LinkError):
        link(store, pr_urls=["https://github.com/o/r/issues/7"])

    assert store.slack_posts() == []


def test_linking_twice_records_each_post_once(store):
    link(store)
    link(store)

    assert len(store.slack_posts()) == 1


def test_nothing_in_the_link_module_calls_a_model():
    """Toolbelt records what it is told: reading Slack is the caller's job."""
    import toolbelt.ui.slack_link as module

    source = Path(module.__file__).read_text()
    assert "claude" not in source.casefold().replace("claude -p", "")
    assert "slack_import" not in source and "slack_proxy" not in source
