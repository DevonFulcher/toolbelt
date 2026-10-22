"""Links a Slack message that was posted by hand to the PRs (tasks) it is about.

``toolbelt slack link <message link>`` records the message as the post (or, for
a reply, the bump) of each PR it names, so the PR reads as posted, its post age
counts from the message and the person it asked is its Assigned Review: exactly
what posting from the card records. The caller names the PRs (``--pr``) and who
was asked (``--ask``); toolbelt reads nothing from Slack, it validates and
records. The posts are written to the stack DB; a running ``tt ui`` notices and
updates its cards.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from toolbelt.git.stack.store import SlackPostRow, StackStore
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_config import SlackConfig

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
    *,
    message_url: str,
    pr_urls: list[str],
    ask: str | None,
) -> list[SlackPostRow]:
    """Records the message as the post of each of ``pr_urls`` (see the module
    docstring) and returns what was recorded."""
    ref = parse_message_url(message_url)
    if not pr_urls:
        raise LinkError("name the PRs the message is about with --pr <GitHub PR link>")
    asked = None
    if ask is not None:
        asked = next((p for p in config.reviewers if p.name == ask), None)
        if asked is None:
            known = ", ".join(p.name for p in config.reviewers)
            raise LinkError(f"{ask!r} is not one of the slack.reviewers ({known})")
    rows = rows_for_prs(ref, [parse_pr(url) for url in pr_urls], asked)
    store.save_slack_posts(rows)
    return rows
