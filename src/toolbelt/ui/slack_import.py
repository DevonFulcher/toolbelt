"""Learns what the user already posted in a Slack channel, so PRs posted by hand
(or before toolbelt posted anything) read as posted.

``SlackReader`` asks a cheap Claude session, allowed only the Slack read tools
(no send tool), for the user's own messages in the channel since a time: their
top-level posts and their replies in threads. ``rows_from_messages`` turns
those into ``slack_posts`` rows: a top-level message is a post of every PR it
links, a reply is a bump of every PR it links, and the person asked is the
``<@id>`` mentioned on the PR's line (else the message's first mention).

The ``claude`` call is a seam (``Runner``), as in ``slack_proxy``.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from toolbelt.git.stack.store import SlackPostRow
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_proxy import Runner, SlackProxyError, claude_runner, parse_answer

READ_CHANNEL_TOOL = "mcp__claude_ai_Slack__slack_read_channel"
READ_THREAD_TOOL = "mcp__claude_ai_Slack__slack_read_thread"

#: How much of a long message the session returns: PR links come first.
MAX_TEXT = 1500

_PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_MENTION = re.compile(r"<@(\w+)(?:\|([^>]*))?>")


@dataclass(frozen=True)
class ChannelMessage:
    ts: str
    #: The message this one replies to (``None`` for a top-level message).
    thread_ts: str | None
    text: str


@dataclass(frozen=True)
class ChannelRead:
    """The user's messages, and what the session that read them cost."""

    messages: list[ChannelMessage]
    cost_usd: float
    session_id: str | None


@dataclass(frozen=True)
class MessageRead:
    """One message, and what the session that read it cost."""

    message: ChannelMessage
    cost_usd: float
    session_id: str | None


class SlackReader:
    def __init__(self, runner: Runner = claude_runner) -> None:
        self._runner = runner

    async def own_messages(
        self, *, channel: str, user_id: str, since: datetime
    ) -> ChannelRead:
        """``user_id``'s messages in ``channel`` since ``since`` (top-level and
        thread replies), and what reading them cost."""
        output = await self._runner(
            ["--allowedTools", READ_CHANNEL_TOOL, READ_THREAD_TOOL],
            _read_prompt(channel, user_id, int(since.timestamp())),
        )
        session = parse_answer(output)
        messages = []
        for post in session.data.get("posts", []):
            messages.append(ChannelMessage(str(post["ts"]), None, str(post["text"])))
            for reply in post.get("replies", []):
                messages.append(
                    ChannelMessage(
                        str(reply["ts"]), str(post["ts"]), str(reply["text"])
                    )
                )
        return ChannelRead(messages, session.cost_usd, session.session_id)

    async def read_message(
        self, *, channel: str, ts: str, thread_ts: str | None = None
    ) -> MessageRead:
        """The message at ``ts`` in ``channel``. ``thread_ts`` is the first
        message of its thread when it is a reply (a reply is found through its
        thread)."""
        output = await self._runner(
            ["--allowedTools", READ_THREAD_TOOL],
            _read_message_prompt(channel, ts, thread_ts),
        )
        session = parse_answer(output)
        if "error" in session.data:
            raise SlackProxyError(
                f"Slack would not show the message: {session.data['error']}"
            )
        found_ts = str(session.data.get("ts") or ts)
        parent = str(session.data.get("thread_ts") or found_ts)
        return MessageRead(
            ChannelMessage(
                found_ts,
                None if parent == found_ts else parent,
                str(session.data["text"]),
            ),
            session.cost_usd,
            session.session_id,
        )


def _read_message_prompt(channel: str, ts: str, thread_ts: str | None) -> str:
    return f"""Read Slack and change nothing. Do exactly these steps.

1. Call the Slack tool slack_read_thread once with channel_id "{channel}" and message_ts "{thread_ts or ts}" (limit 100, response_format "concise").
2. Find the message whose ts is "{ts}" in what it returned.
3. Answer with only a JSON object: {{"ts": "<that message's ts>", "thread_ts": "<the ts of the first message of its thread>", "text": "<the message text exactly as Slack returned it, cut to its first {MAX_TEXT} characters>"}}. If you cannot find it, answer {{"error": "<why>"}}.
"""


def _read_prompt(channel: str, user_id: str, oldest: int) -> str:
    return f"""Read Slack and change nothing. Do exactly these steps.

1. Call the Slack tool slack_read_channel on channel_id "{channel}" for messages posted since unix time {oldest} (use its oldest / after parameter if it has one; page until you reach that time). Keep only top-level messages written by user {user_id}.
2. For each of those that has replies, call slack_read_thread and keep the replies written by user {user_id}.
3. Answer with only a JSON object: {{"posts": [{{"ts": "<message ts>", "text": "<the message text exactly as Slack returned it, cut to its first {MAX_TEXT} characters>", "replies": [{{"ts": "<reply ts>", "text": "<same>"}}]}}]}}. Use an empty replies list when there are none.
"""


def rows_from_messages(
    channel: str, messages: list[ChannelMessage], reviewers: tuple[Person, ...]
) -> list[SlackPostRow]:
    """One row per PR each message links (see the module docstring)."""
    known = {person.id: person.name for person in reviewers}
    rows = []
    for message in messages:
        mentions = _MENTION.findall(message.text)
        fallback = mentions[0] if mentions else None
        seen: set[tuple[str, int]] = set()
        for line in message.text.split("\n"):
            line_mentions = _MENTION.findall(line)
            for found_repo, number in _PR_URL.findall(line):
                repo = found_repo.lower()
                pr = (repo, int(number))
                if pr in seen:
                    continue
                seen.add(pr)
                asked = line_mentions[0] if line_mentions else fallback
                rows.append(
                    SlackPostRow(
                        channel=channel,
                        ts=message.ts,
                        thread_ts=message.thread_ts or message.ts,
                        repo=repo,
                        pr_number=pr[1],
                        kind="bump" if message.thread_ts else "post",
                        posted_at=datetime.fromtimestamp(float(message.ts), UTC),
                        assignee_id=asked[0] if asked else None,
                        assignee_name=(
                            known.get(asked[0]) or asked[1] or None if asked else None
                        ),
                    )
                )
    return rows
