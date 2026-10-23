"""Sends a Slack message through a cheap Claude session.

Slack reaches Claude Code through the claude.ai Slack connector, which only a
model session can call (a local client needs a Slack app of its own). So a
post is made by a headless ``claude -p --model haiku`` session that is allowed
one tool, ``slack_send_message``, and told to post exactly the text it is
given. The same session then reads the message back with ``slack_read_thread``
and returns what Slack holds, so the caller can check that the text arrived
(Slack rewrites the formatting, so what is checked is that every PR link and
mention is in it).

Every call costs a few cents (mostly the session's context): ``cost_usd`` on
the result says how much.

The ``claude`` call is a seam (``Runner``) so tests inject canned output.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from toolbelt import logged_process

MODEL = "haiku"
SEND_TOOL = "mcp__claude_ai_Slack__slack_send_message"
READ_TOOL = "mcp__claude_ai_Slack__slack_read_thread"
TIMEOUT_SECONDS = 240.0

#: (extra ``claude`` arguments, the prompt) -> what ``claude`` printed.
Runner = Callable[[list[str], str], Awaitable[str]]

_LINK_TS = re.compile(r"/p(\d{10})(\d{6})")


class SlackProxyError(RuntimeError):
    """The send failed, or it is not known whether it happened."""


@dataclass(frozen=True)
class SessionAnswer:
    """What a ``claude`` session answered: the JSON object, what the session
    cost and its id (the key to its transcript and its spend)."""

    data: dict
    cost_usd: float
    session_id: str | None


@dataclass(frozen=True)
class SentMessage:
    link: str
    #: The message's own timestamp, and its thread's (the same for a post).
    ts: str
    thread_ts: str
    #: What Slack held for the message when it was read back (``None`` if the
    #: session did not manage to read it).
    read_back: str | None
    cost_usd: float
    session_id: str | None = None


#: ``claude`` gives up with this on stderr when the machine is too busy to hand
#: it the prompt within a few seconds. It exits before it calls any tool, so
#: nothing was sent and running it again cannot post twice.
_STALLED_STDIN = "no stdin data received"
STALL_ATTEMPTS = 3


@dataclass(frozen=True)
class _Finished:
    returncode: int | None
    stdout: bytes
    stderr: bytes


async def _run_claude(args: list[str], prompt: str) -> _Finished:
    process = await logged_process.create_subprocess_exec(
        "claude",
        "-p",
        "--model",
        MODEL,
        "--output-format",
        "json",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(prompt.encode()), TIMEOUT_SECONDS
        )
    except TimeoutError as timeout:
        process.kill()
        raise SlackProxyError(
            "the Slack session timed out: check Slack, the message may have been sent"
        ) from timeout
    return _Finished(process.returncode, stdout, stderr)


async def claude_runner(args: list[str], prompt: str) -> str:
    for attempt in range(1, STALL_ATTEMPTS + 1):
        finished = await _run_claude(args, prompt)
        stalled = _STALLED_STDIN in finished.stderr.decode().lower()
        if finished.returncode != 0 and stalled and attempt < STALL_ATTEMPTS:
            continue
        break
    if finished.returncode != 0:
        raise SlackProxyError(
            f"claude failed: {finished.stderr.decode().strip()[:300]}"
        )
    return finished.stdout.decode()


def _send_prompt(channel: str, text: str, thread_ts: str | None) -> str:
    thread = f'\n  thread_ts: "{thread_ts}"' if thread_ts else ""
    read_ts = thread_ts or "the timestamp of the message you sent"
    return f"""Do exactly these steps and nothing else.

1. Call the Slack tool slack_send_message once with:
  channel_id: "{channel}"
  message: the text between the lines BEGIN and END below, exactly as written, character for character (without the BEGIN and END lines). Change nothing, add nothing, do not reformat.{thread}
  unfurl_app_links: false
2. Call slack_read_thread once with channel_id "{channel}" and message_ts "{read_ts}" (limit 100, response_format "concise") and find the message you just sent: the parent message if you posted at the top level, otherwise the last reply by you in the thread.
3. Answer with only a JSON object: {{"link": "<the message link the send returned>", "read_back": "<the text of that message exactly as slack_read_thread returned it>"}}. If the send failed, answer {{"error": "<the error>"}} instead.

BEGIN
{text}
END
"""


def parse_answer(output: str) -> SessionAnswer:
    try:
        envelope = json.loads(output)
    except json.JSONDecodeError as bad:
        raise SlackProxyError(f"claude printed no JSON: {output[:200]}") from bad
    if envelope.get("is_error"):
        raise SlackProxyError(f"claude failed: {str(envelope.get('result'))[:300]}")
    answer = str(envelope.get("result", ""))
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        raise SlackProxyError(f"the session did not answer with JSON: {answer[:200]}")
    try:
        return SessionAnswer(
            data=json.loads(answer[start : end + 1]),
            cost_usd=float(envelope.get("total_cost_usd") or 0.0),
            session_id=envelope.get("session_id"),
        )
    except json.JSONDecodeError as bad:
        raise SlackProxyError(
            f"the session's JSON is malformed: {answer[:200]}"
        ) from bad


class SlackProxy:
    def __init__(self, runner: Runner = claude_runner) -> None:
        self._runner = runner

    async def send(
        self, *, channel: str, text: str, thread_ts: str | None = None
    ) -> SentMessage:
        """Posts ``text`` to ``channel`` (a reply when ``thread_ts`` is given)
        and reads it back."""
        output = await self._runner(
            ["--allowedTools", SEND_TOOL, READ_TOOL],
            _send_prompt(channel, text, thread_ts),
        )
        session = parse_answer(output)
        answer = session.data
        if "error" in answer:
            raise SlackProxyError(f"Slack refused the message: {answer['error']}")
        link = str(answer.get("link", ""))
        match = _LINK_TS.search(link)
        if match is None:
            raise SlackProxyError(
                f"the session did not return a message link (got {link!r}): "
                "check Slack, the message may have been sent"
            )
        ts = f"{match.group(1)}.{match.group(2)}"
        read_back = answer.get("read_back")
        return SentMessage(
            link=link,
            ts=ts,
            thread_ts=thread_ts or ts,
            read_back=str(read_back) if read_back else None,
            cost_usd=session.cost_usd,
            session_id=session.session_id,
        )
