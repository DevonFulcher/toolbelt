"""Reading the user's own Slack posts and turning them into recorded posts. The
`claude` call is faked: nothing here reaches Slack."""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_import import (
    READ_CHANNEL_TOOL,
    READ_THREAD_TOOL,
    ChannelMessage,
    SlackReader,
    rows_from_messages,
)
from toolbelt.ui.slack_proxy import SlackProxyError

ALAN, WILL = Person("alan", "UA"), Person("will", "UW")
POST_TS = "1792529791.708329"
STACK_POST = (
    "- <https://github.com/o/r/pull/1|One> <@UA|alan.andradecestelos> `+3/-1`\n"
    "    - <https://github.com/o/r/pull/2|Two> <@UA|alan.andradecestelos> `+1/-1`\n"
    "- <https://github.com/o/r/pull/3|Three> <@UW|will.james> `+9/-9`\n"
    "Thanks!"
)


def envelope(answer: dict | str, cost: float = 0.058) -> str:
    result = answer if isinstance(answer, str) else json.dumps(answer)
    return json.dumps(
        {
            "is_error": False,
            "result": result,
            "total_cost_usd": cost,
            "session_id": "read-session",
        }
    )


class Runner:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[list[str], str]] = []

    async def __call__(self, args: list[str], prompt: str) -> str:
        self.calls.append((args, prompt))
        return self.output


def read(output: str):
    runner = Runner(output)
    since = datetime(2026, 10, 17, tzinfo=UTC)
    result = asyncio.run(
        SlackReader(runner).own_messages(channel="CREVIEW", user_id="UDEV", since=since)
    )
    return result, runner


# ---- the reader ----------------------------------------------------------------


def test_the_session_may_only_read_never_send():
    _, runner = read(envelope({"posts": []}))

    args, prompt = runner.calls[0]
    assert args == ["--allowedTools", READ_CHANNEL_TOOL, READ_THREAD_TOOL]
    assert "send" not in " ".join(args)
    assert 'channel_id "CREVIEW"' in prompt and "user UDEV" in prompt
    assert str(int(datetime(2026, 10, 17, tzinfo=UTC).timestamp())) in prompt


def test_posts_and_their_replies_come_back_as_messages_of_their_thread():
    (read_result), _ = read(
        envelope(
            {
                "posts": [
                    {
                        "ts": POST_TS,
                        "text": "a post",
                        "replies": [{"ts": "1792600000.000200", "text": "a reply"}],
                    },
                    {"ts": "1792700000.000100", "text": "another", "replies": []},
                ]
            }
        )
    )

    assert read_result.messages == [
        ChannelMessage(POST_TS, None, "a post"),
        ChannelMessage("1792600000.000200", POST_TS, "a reply"),
        ChannelMessage("1792700000.000100", None, "another"),
    ]
    assert read_result.cost_usd == 0.058
    assert read_result.session_id == "read-session"


def test_a_session_that_does_not_answer_with_json_is_an_error():
    with pytest.raises(SlackProxyError):
        read(envelope("I could not read the channel"))


# ---- the rows --------------------------------------------------------------------


def rows(*messages: ChannelMessage):
    return rows_from_messages("CREVIEW", list(messages), (ALAN, WILL))


def test_a_top_level_message_is_a_post_of_every_pr_it_links_asking_who_is_on_its_line():
    result = rows(ChannelMessage(POST_TS, None, STACK_POST))

    assert [(r.pr_number, r.assignee_name, r.kind) for r in result] == [
        (1, "alan", "post"),
        (2, "alan", "post"),
        (3, "will", "post"),
    ]
    assert {r.thread_ts for r in result} == {POST_TS}
    assert {r.ts for r in result} == {POST_TS}
    assert {r.channel for r in result} == {"CREVIEW"}
    assert result[0].posted_at == datetime.fromtimestamp(float(POST_TS), UTC)
    assert result[0].assignee_id == "UA"


def test_a_reply_is_a_bump_in_its_threads_and_asks_the_first_person_mentioned():
    result = rows(
        ChannelMessage(
            "1792600000.000200",
            POST_TS,
            "<@UW|will.james> could you look?\nhttps://github.com/o/r/pull/7",
        )
    )

    assert [(r.pr_number, r.kind, r.thread_ts, r.assignee_name) for r in result] == [
        (7, "bump", POST_TS, "will")
    ]
    assert result[0].ts == "1792600000.000200"


def test_a_person_who_is_not_a_configured_reviewer_is_named_as_slack_labels_them():
    result = rows(
        ChannelMessage(POST_TS, None, "<https://github.com/o/r/pull/1|x> <@UX|someone>")
    )

    assert (result[0].assignee_id, result[0].assignee_name) == ("UX", "someone")


def test_a_message_without_a_mention_asks_nobody():
    result = rows(ChannelMessage(POST_TS, None, "https://github.com/o/r/pull/1"))

    assert (result[0].assignee_id, result[0].assignee_name) == (None, None)


def test_a_pr_linked_twice_in_a_message_is_recorded_once():
    result = rows(
        ChannelMessage(
            POST_TS,
            None,
            "https://github.com/o/r/pull/1\nsee again https://github.com/o/r/pull/1",
        )
    )

    assert len(result) == 1


def test_a_message_that_links_no_pr_records_nothing():
    assert rows(ChannelMessage(POST_TS, None, "thanks all!")) == []


def test_prs_of_different_repos_with_the_same_number_are_kept_apart():
    result = rows(
        ChannelMessage(
            POST_TS,
            None,
            "https://github.com/o/a/pull/5\nhttps://github.com/o/b/pull/5",
        )
    )

    assert [(r.repo, r.pr_number) for r in result] == [("o/a", 5), ("o/b", 5)]


# ---- reading one message by its link --------------------------------------------------


def read_message(output: str, **kwargs):
    runner = Runner(output)
    result = asyncio.run(
        SlackReader(runner).read_message(
            channel="CREVIEW", ts="1792696217.666559", **kwargs
        )
    )
    return result, runner


def test_a_message_is_read_through_its_thread_with_only_the_read_tool():
    result, runner = read_message(
        envelope(
            {
                "ts": "1792696217.666559",
                "thread_ts": "1792696217.666559",
                "text": "hello",
            }
        )
    )

    args, prompt = runner.calls[0]
    assert args == ["--allowedTools", READ_THREAD_TOOL]
    assert (
        'channel_id "CREVIEW"' in prompt and 'message_ts "1792696217.666559"' in prompt
    )
    assert result.message == ChannelMessage("1792696217.666559", None, "hello")
    assert (result.cost_usd, result.session_id) == (0.058, "read-session")


def test_a_reply_is_found_through_its_threads_first_message():
    result, runner = read_message(
        envelope(
            {
                "ts": "1792696217.666559",
                "thread_ts": "1792600000.000100",
                "text": "bump",
            }
        ),
        thread_ts="1792600000.000100",
    )

    assert 'message_ts "1792600000.000100"' in runner.calls[0][1]
    assert result.message.thread_ts == "1792600000.000100"


def test_a_message_slack_would_not_show_is_an_error():
    with pytest.raises(SlackProxyError, match="would not show the message: not found"):
        read_message(envelope({"error": "not found"}))
