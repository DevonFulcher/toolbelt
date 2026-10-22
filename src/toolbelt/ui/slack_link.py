"""Links a Slack message that was posted by hand to the PRs (tasks) it is about.

``toolbelt slack link <message link>`` records the message as the post (or, for
a reply, the bump) of each PR it names, so the PR reads as posted, its post age
counts from the message and the person it asked is its Assigned Review: exactly
what posting from the card records. The PRs are the ones given with ``--pr`` or,
when none is given, the ones the message links, which a read-only Slack session
reads out of it. The posts are written to the stack DB; a running ``tt ui``
notices and updates its cards.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from toolbelt.git.stack.store import SlackPostRow, SlackSessionRow, StackStore
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_config import SlackConfig
from toolbelt.ui.slack_import import SlackReader, rows_from_messages

_MESSAGE_PATH = re.compile(
    r"/archives/(?P<channel>[A-Z0-9]+)/p(?P<seconds>\d{10})(?P<micros>\d{6})"
)
_PR_URL = re.compile(r"github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<number>\d+)")


class LinkError(Exception):
    """The message or a PR could not be understood; the text says why."""


@dataclass(frozen=True)
class MessageRef:
    channel: str
    ts: str
    #: The first message of the thread when the link is to a reply.
    thread_ts: str | None


def parse_message_url(url: str) -> MessageRef:
    """The channel, timestamp and thread of a Slack message link
    (``https://<workspace>.slack.com/archives/<channel>/p<ts>``)."""
    parts = urlsplit(url)
    found = _MESSAGE_PATH.search(parts.path)
    if found is None:
        raise LinkError(
            f"{url!r} is not a link to a Slack message "
            "(https://<workspace>.slack.com/archives/<channel>/p<digits>)"
        )
    ts = f"{found['seconds']}.{found['micros']}"
    thread_ts = parse_qs(parts.query).get("thread_ts", [None])[0]
    return MessageRef(found["channel"], ts, thread_ts if thread_ts != ts else None)


def parse_pr(text: str) -> tuple[str, int]:
    """``(owner/name, number)`` of a PR given as its GitHub URL."""
    found = _PR_URL.search(text)
    if found is None:
        raise LinkError(
            f"{text!r} is not a link to a GitHub PR (https://github.com/<owner>/<repo>/pull/<number>)"
        )
    return found["repo"].lower(), int(found["number"])


def rows_for_prs(
    ref: MessageRef, prs: list[tuple[str, int]], asked: Person | None
) -> list[SlackPostRow]:
    """The posts that link ``prs`` to the message: a post, or a bump for a reply."""
    return [
        SlackPostRow(
            channel=ref.channel,
            ts=ref.ts,
            thread_ts=ref.thread_ts or ref.ts,
            repo=repo,
            pr_number=number,
            kind="bump" if ref.thread_ts else "post",
            posted_at=datetime.fromtimestamp(float(ref.ts), UTC),
            assignee_id=asked.id if asked else None,
            assignee_name=asked.name if asked else None,
        )
        for repo, number in prs
    ]


def link_message(
    store: StackStore,
    config: SlackConfig,
    reader: SlackReader,
    *,
    message_url: str,
    pr_urls: list[str],
    ask: str | None,
    now: datetime,
) -> list[SlackPostRow]:
    """Records the message as the post of each PR (see the module docstring) and
    returns what was recorded."""
    ref = parse_message_url(message_url)
    asked = None
    if ask is not None:
        asked = next((p for p in config.reviewers if p.name == ask), None)
        if asked is None:
            known = ", ".join(p.name for p in config.reviewers)
            raise LinkError(f"{ask!r} is not one of the slack.reviewers ({known})")
    if pr_urls:
        rows = rows_for_prs(ref, [parse_pr(url) for url in pr_urls], asked)
    else:
        read = asyncio.run(
            reader.read_message(channel=ref.channel, ts=ref.ts, thread_ts=ref.thread_ts)
        )
        if read.session_id:
            store.add_slack_session(
                SlackSessionRow(read.session_id, now, "import", read.cost_usd)
            )
        rows = rows_from_messages(ref.channel, [read.message], config.reviewers)
        if asked is not None:
            rows = [
                SlackPostRow(
                    **{
                        **row.__dict__,
                        "assignee_id": asked.id,
                        "assignee_name": asked.name,
                    }
                )
                for row in rows
            ]
        if not rows:
            raise LinkError(
                "The message links no GitHub PR: name them with --pr "
                "https://github.com/<owner>/<repo>/pull/<number>"
            )
    store.save_slack_posts(rows)
    return rows
