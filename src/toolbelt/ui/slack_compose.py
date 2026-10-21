"""The Slack messages for a PR: a post that asks for review and the bump that
follows when nobody has looked.

Text is composed the way the ``pr-publish`` skill writes it (a first line saying
an AI wrote it on Devon's behalf, markdown links and ``-`` bullets that Slack
renders, a stack indented under the PR it is stacked on). A person is written
``@name`` so the dialog shows what Slack will show; ``to_slack`` turns each
``@name`` into the ``<@id>`` mention Slack needs when the message is sent.

Pure functions: nothing here reads Slack or GitHub.
"""

import re
from dataclasses import dataclass

ATTRIBUTION = "🤖 AI-generated on behalf of Devon:"

_TICKET_KEY = re.compile(r"^\s*[A-Z][A-Z0-9]+-\d+\s*[:\-–]?\s*")


@dataclass(frozen=True)
class Person:
    """Someone to ask for a review: their Slack display name and user ID."""

    name: str
    id: str


@dataclass(frozen=True)
class PostPr:
    """A PR in a post."""

    number: int
    url: str
    title: str
    additions: int
    deletions: int
    assignee: Person | None
    #: The number of the PR this one is stacked on, when that PR is in the
    #: same post (it is then indented under it).
    parent_number: int | None = None


def short_context(title: str) -> str:
    """A few words on what the PR does: its title without the ticket key,
    starting with a capital."""
    context = _TICKET_KEY.sub("", title).strip()
    return context[:1].upper() + context[1:]


def _mention(person: Person | None) -> str:
    return f" @{person.name}" if person else ""


def compose_post(prs: list[PostPr]) -> str:
    """One top-level message for newly published PRs, bottom of a stack first."""
    depth: dict[int, int] = {}
    for pr in prs:
        parent = pr.parent_number
        depth[pr.number] = depth[parent] + 1 if parent in depth else 0
    bullets = [
        f"{'  ' * depth[pr.number]}- [{short_context(pr.title)}]({pr.url})"
        f"{_mention(pr.assignee)} `+{pr.additions}/-{pr.deletions}`"
        for pr in prs
    ]
    return "\n".join([ATTRIBUTION, *bullets, "", "Thanks for your time!"])


def compose_bump(urls: list[str], assignee: Person | None) -> str:
    """A threaded reply under the original post for PRs nobody has looked at."""
    if len(urls) == 1:
        subject = f"{urls[0]} has"
    else:
        subject = f"{', '.join(urls[:-1])} and {urls[-1]} have"
    ask = f"{assignee.name}, could you" if assignee else "Could someone"
    text = (
        f"It has been over 24 hours and {subject} no comments or reviews yet. "
        f"{'@' if assignee else ''}{ask} take a look"
        f"{' instead' if assignee else ''}? Thanks!"
    )
    return f"{ATTRIBUTION}\n{text}"


def to_slack(text: str, mentions: dict[str, str]) -> str:
    """``text`` with each ``@name`` (a key of ``mentions``) as Slack's
    ``<@id>``, and the attribution line first when it is missing."""
    for name in sorted(mentions, key=len, reverse=True):
        text = re.sub(
            rf"(?<![\w@])@{re.escape(name)}(?![\w-]|\.\w)",
            f"<@{mentions[name]}>",
            text,
        )
    if not text.startswith(ATTRIBUTION):
        text = f"{ATTRIBUTION}\n{text}"
    return text
