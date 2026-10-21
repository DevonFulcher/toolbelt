"""Drafts and sends the Slack messages for PRs: what the card's Post in Slack
and Bump in Slack buttons do.

``draft`` writes the message toolbelt would post for a branch (a post for the
branch and the PRs of its stack that are ready and not posted yet, or a bump
for the PRs of one thread that nobody has looked at) and who to ask. ``send``
posts the text the user confirmed (possibly edited), through ``SlackProxy``,
and records it so the PR reads as posted and its post age starts: the posts
are the source of truth, so nothing reads Slack to find out.

The channel is always the configured one. The client never names it.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from toolbelt.git.stack.store import Parents, SlackPostRow
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import Fetched, TrackedBranch
from toolbelt.ui.slack_assignee import choose_assignee
from toolbelt.ui.slack_compose import (
    Person,
    PostPr,
    compose_bump,
    compose_post,
    to_slack,
)
from toolbelt.ui.slack_config import SlackConfig
from toolbelt.ui.slack_proxy import SentMessage
from toolbelt.ui.slack_tracker import SlackTracker
from toolbelt.ui.snapshot import descendants


class SlackNotApplicable(Exception):
    """The branch has nothing to post or bump (or already has it)."""


class SlackUnknown(Exception):
    """A branch or PR the message names is not tracked."""


@dataclass(frozen=True)
class _Pr:
    """A tracked branch's PR, as the message needs it."""

    repo_id: int
    branch: str
    repo: str
    number: int
    url: str
    title: str
    additions: int
    deletions: int


Sender = Callable[..., Awaitable[SentMessage]]


class SlackService:
    def __init__(
        self,
        *,
        config: SlackConfig,
        tracker: SlackTracker,
        send_message: Sender,
        branches: Callable[[], list[TrackedBranch]],
        fetched: Callable[[int, str], Fetched | None],
        parents: Callable[[int], Parents],
        slug_of: Callable[[int], str | None],
        next_kind: Callable[[int, str], str | None],
        posts: Callable[[], list[SlackPostRow]],
        save: Callable[[list[SlackPostRow]], None],
        wall_clock: Callable[[], datetime],
        changed: Callable[[set[tuple[int, str]]], None],
    ) -> None:
        """``send_message(channel=, text=, thread_ts=)`` posts (``SlackProxy.send``);
        ``next_kind`` is a branch's next action; ``changed`` is told which
        branches' Slack standing changed."""
        self._config = config
        self._tracker = tracker
        self._send = send_message
        self._branches = branches
        self._fetched = fetched
        self._parents = parents
        self._slug_of = slug_of
        self._next_kind = next_kind
        self._posts = posts
        self._save = save
        self._wall_clock = wall_clock
        self._changed = changed

    # ---- reading what is tracked -------------------------------------------

    def _pr(self, repo_id: int, branch: str) -> _Pr | None:
        fetched = self._fetched(repo_id, branch)
        slug = self._slug_of(repo_id)
        if (
            fetched is None
            or fetched.facts is None
            or fetched.status is None
            or not slug
        ):
            return None
        facts = fetched.facts
        return _Pr(
            repo_id=repo_id,
            branch=branch,
            repo=slug,
            number=facts.number,
            url=fetched.status.url or f"https://github.com/{slug}/pull/{facts.number}",
            title=facts.title,
            additions=facts.additions,
            deletions=facts.deletions,
        )

    def _pr_of(self, ref: api.SlackPrRef) -> _Pr:
        for branch in self._branches():
            pr = self._pr(branch.repo_id, branch.branch)
            if pr is not None and (pr.repo, pr.number) == (ref.repo, ref.number):
                return pr
        raise SlackUnknown(f"{ref.repo}#{ref.number} is not a tracked PR")

    def _person(self, person_id: str | None) -> Person | None:
        return next((p for p in self._config.reviewers if p.id == person_id), None)

    # ---- the draft -----------------------------------------------------------

    def draft(
        self, repo_id: int, branch: str, assignee_id: str | None
    ) -> api.SlackDraft:
        kind = self._next_kind(repo_id, branch)
        if kind == "post_slack":
            return self._draft_post(repo_id, branch, assignee_id)
        if kind == "bump_slack":
            return self._draft_bump(repo_id, branch, assignee_id)
        raise SlackNotApplicable(
            "Nothing to post or bump: the branch's next action is not about Slack."
        )

    def _draft_post(
        self, repo_id: int, branch: str, assignee_id: str | None
    ) -> api.SlackDraft:
        parents = self._parents(repo_id)
        above: list[str] = []
        current = branch
        while parents.get(current) in parents:
            current = parents[current]
            above.append(current)
        order = [*reversed(above)]
        order += descendants(parents, branch)
        prs = [
            pr
            for name in order
            if self._next_kind(repo_id, name) == "post_slack"
            and (pr := self._pr(repo_id, name)) is not None
        ]
        numbers = {pr.branch: pr.number for pr in prs}
        history = self._posts()
        lines = sum(pr.additions + pr.deletions for pr in prs)
        chosen = self._person(assignee_id) or choose_assignee(
            self._config, repo=prs[0].repo, lines=lines, history=history
        )
        text = compose_post(
            [
                PostPr(
                    number=pr.number,
                    url=pr.url,
                    title=pr.title,
                    additions=pr.additions,
                    deletions=pr.deletions,
                    assignee=chosen,
                    parent_number=numbers.get(parents.get(pr.branch, "")),
                )
                for pr in prs
            ]
        )
        return self._draft_of("post", text, chosen, prs, None)

    def _draft_bump(
        self, repo_id: int, branch: str, assignee_id: str | None
    ) -> api.SlackDraft:
        pr = self._pr(repo_id, branch)
        assert pr is not None
        history = self._posts()
        last = _last_post(history, pr)
        assert last is not None
        due = self._bump_due_in_thread(last.channel, last.thread_ts, history)
        asked = frozenset(
            row.assignee_name
            for row in history
            if row.assignee_name
            and row.channel == last.channel
            and row.thread_ts == last.thread_ts
        )
        chosen = self._person(assignee_id) or choose_assignee(
            self._config,
            repo=pr.repo,
            lines=pr.additions + pr.deletions,
            history=history,
            bump=True,
            exclude=asked,
        )
        text = compose_bump([p.url for p in due], chosen)
        return self._draft_of("bump", text, chosen, due, last.thread_ts)

    def _bump_due_in_thread(
        self, channel: str, thread_ts: str, history: list[SlackPostRow]
    ) -> list[_Pr]:
        """The PRs whose latest post is in this thread and whose bump is due."""
        now = self._wall_clock()
        due = []
        for tracked in self._branches():
            pr = self._pr(tracked.repo_id, tracked.branch)
            if pr is None:
                continue
            last = _last_post(history, pr)
            state = self._tracker.state(
                tracked.repo_id, tracked.branch, self._fetched(*tracked.key), now
            )
            if (
                last is not None
                and (last.channel, last.thread_ts) == (channel, thread_ts)
                and state is not None
                and state.bump_due
            ):
                due.append(pr)
        return due

    def _draft_of(
        self,
        kind: str,
        text: str,
        chosen: Person | None,
        prs: list[_Pr],
        thread_ts: str | None,
    ) -> api.SlackDraft:
        return api.SlackDraft(
            kind=kind,  # type: ignore[arg-type]
            channel=self._config.channel,
            text=text,
            assignee=_person_model(chosen),
            candidates=[
                api.SlackPerson(id=p.id, name=p.name) for p in self._config.reviewers
            ],
            prs=[
                api.SlackDraftPr(
                    repo=pr.repo,
                    number=pr.number,
                    url=pr.url,
                    title=pr.title,
                    additions=pr.additions,
                    deletions=pr.deletions,
                )
                for pr in prs
            ],
            thread_ts=thread_ts,
        )

    # ---- sending ---------------------------------------------------------------

    async def send(self, request: api.SlackSendRequest) -> api.SlackSent:
        prs = [self._pr_of(ref) for ref in request.prs]
        if not prs:
            raise SlackNotApplicable("The message names no PR.")
        history = self._posts()
        now = self._wall_clock()
        for pr in prs:
            last = _last_post(history, pr)
            if request.kind == "post" and last is not None:
                raise SlackNotApplicable(
                    f"{pr.repo}#{pr.number} is already posted in Slack."
                )
            if request.kind == "bump" and (
                last is None or last.thread_ts != request.thread_ts
            ):
                raise SlackNotApplicable(
                    f"{pr.repo}#{pr.number} was not posted in that thread."
                )
        mentions = {p.name: p.id for p in self._config.reviewers}
        text = to_slack(request.text, mentions)
        sent = await self._send(
            channel=self._config.channel,
            text=text,
            thread_ts=request.thread_ts if request.kind == "bump" else None,
        )
        person = self._person(request.assignee_id)
        self._save(
            [
                SlackPostRow(
                    channel=self._config.channel,
                    ts=sent.ts,
                    thread_ts=sent.thread_ts,
                    repo=pr.repo,
                    pr_number=pr.number,
                    kind=request.kind,
                    posted_at=now,
                    assignee_id=person.id if person else None,
                    assignee_name=person.name if person else None,
                )
                for pr in prs
            ]
        )
        self._tracker.reload()
        self._changed({(pr.repo_id, pr.branch) for pr in prs})
        warning = _verify(text, sent)
        return api.SlackSent(
            link=sent.link,
            verified=warning is None,
            warning=warning,
            cost_usd=sent.cost_usd,
        )


def _last_post(history: list[SlackPostRow], pr: _Pr) -> SlackPostRow | None:
    rows = [r for r in history if (r.repo, r.pr_number) == (pr.repo, pr.number)]
    return max(rows, key=lambda r: r.posted_at) if rows else None


def _person_model(person: Person | None) -> api.SlackPerson | None:
    return api.SlackPerson(id=person.id, name=person.name) if person else None


def _verify(text: str, sent: SentMessage) -> str | None:
    """``None`` when Slack's copy holds every PR link and mention of the text
    that was sent, else why it is not known to."""
    if not sent.read_back:
        return "Posted, but the message could not be read back to check it."
    expected = sorted(
        set(re.findall(r"https://github\.com/[^\s|>)]+/pull/\d+", text))
    ) + sorted(set(re.findall(r"<@(\w+)>", text)))
    missing = [token for token in expected if token not in sent.read_back]
    if missing:
        return f"Posted, but Slack's copy is missing: {', '.join(missing)}"
    return None
