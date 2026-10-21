"""Posting to Slack through a restricted Haiku session: what it is told, what
it answers, and the errors that must not look like a success. The `claude`
call is faked: nothing here reaches Slack."""

import asyncio
import json

import pytest

from toolbelt.ui.slack_proxy import SlackProxy, SlackProxyError

LINK = "https://dbtlabs.slack.com/archives/D04460UT8GK/p1792600000123456"


def envelope(answer: dict | str, *, cost: float = 0.045, is_error: bool = False) -> str:
    result = answer if isinstance(answer, str) else json.dumps(answer)
    return json.dumps({"is_error": is_error, "result": result, "total_cost_usd": cost})


class Runner:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[list[str], str]] = []

    async def __call__(self, args: list[str], prompt: str) -> str:
        self.calls.append((args, prompt))
        return self.output


def send(output: str, **kwargs):
    runner = Runner(output)
    sent = asyncio.run(
        SlackProxy(runner).send(channel="D04460UT8GK", text="hello <@U1>", **kwargs)
    )
    return sent, runner


def test_a_post_returns_its_link_timestamp_and_what_slack_held():
    sent, _ = send(envelope({"link": LINK, "read_back": "hello <@U1|devon>"}))

    assert sent.link == LINK
    assert sent.ts == "1792600000.123456"
    assert sent.thread_ts == sent.ts  # a top-level post starts its own thread
    assert sent.read_back == "hello <@U1|devon>"
    assert sent.cost_usd == 0.045


def test_a_reply_keeps_the_thread_it_was_sent_to():
    sent, runner = send(
        envelope({"link": LINK + "?thread_ts=1792500000.000100", "read_back": "x"}),
        thread_ts="1792500000.000100",
    )

    assert sent.thread_ts == "1792500000.000100"
    assert sent.ts == "1792600000.123456"
    assert 'thread_ts: "1792500000.000100"' in runner.calls[0][1]


def test_the_session_may_only_send_and_read_a_thread_and_is_given_the_exact_text():
    _, runner = send(envelope({"link": LINK, "read_back": "x"}))

    args, prompt = runner.calls[0]
    assert args == [
        "--allowedTools",
        "mcp__claude_ai_Slack__slack_send_message",
        "mcp__claude_ai_Slack__slack_read_thread",
    ]
    assert 'channel_id: "D04460UT8GK"' in prompt
    assert "BEGIN\nhello <@U1>\nEND" in prompt
    assert "unfurl_app_links: false" in prompt


def test_an_answer_wrapped_in_a_code_fence_is_still_read():
    fenced = "```json\n" + json.dumps({"link": LINK, "read_back": "x"}) + "\n```"

    sent, _ = send(envelope(fenced))

    assert sent.ts == "1792600000.123456"


def test_the_session_not_reading_the_message_back_leaves_it_unread():
    sent, _ = send(envelope({"link": LINK}))

    assert sent.read_back is None


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (envelope({"error": "channel_not_found"}), "channel_not_found"),
        (envelope({"read_back": "x"}), "did not return a message link"),
        (
            envelope({"link": "https://example.com/nope"}),
            "did not return a message link",
        ),
        (envelope("I could not do that."), "did not answer with JSON"),
        (envelope("{not json}"), "malformed"),
        (envelope("boom", is_error=True), "claude failed"),
        ("not json at all", "printed no JSON"),
    ],
)
def test_anything_but_a_clear_success_is_an_error(output, message):
    with pytest.raises(SlackProxyError, match=message):
        send(output)
