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

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from toolbelt.git.stack.status import PrState
from toolbelt.git.stack.store import Parents, SlackPostRow, SlackSessionRow
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import Fetched, TrackedBranch
from toolbelt.ui.slack_assignee import choose_assignee
from toolbelt.ui.slack_compose import (
    BumpPr,
    Person,
    PostPr,
    compose_bump,
    compose_post,
    compose_reassign,
    to_slack,
)
from toolbelt.ui.slack_config import SlackConfig
from toolbelt.ui.slack_proxy import SentMessage
from toolbelt.ui.slack_tracker import SlackTracker
from toolbelt.ui.snapshot import descendants

LOGGER = logging.getLogger(__name__)


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
        next_action: Callable[[int, str], api.NextActionInfo | None],
        posts: Callable[[], list[SlackPostRow]],
        save: Callable[[list[SlackPostRow]], None],
        record_session: Callable[[SlackSessionRow], None],
        wall_clock: Callable[[], datetime],
        changed: Callable[[set[tuple[int, str]]], None],
    ) -> None:
        """``send_message(channel=, text=, thread_ts=)`` posts (``SlackProxy.send``);
        ``next_action`` is a branch's
        next action; ``record_session`` is given each session that read or
        posted, for the spend figures; ``changed`` is told which branches'
        Slack standing changed."""
        self._config = config
        self._tracker = tracker
        self._send = send_message
        self._branches = branches
        self._fetched = fetched
        self._parents = parents
        self._slug_of = slug_of
        self._next_action = next_action
        self._posts = posts
        self._save = save
        self._record_session = record_session
        self._tasks: set[asyncio.Task[None]] = set()
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

    def _next_kind(self, repo_id: int, branch: str) -> str | None:
        action = self._next_action(repo_id, branch)
        return action.kind if action else None

    def draft(
        self,
        repo_id: int,
        branch: str,
        assignee_id: str | None,
        include: list[api.SlackPrRef] | None = None,
        reassign: bool = False,
    ) -> api.SlackDraft:
        """``include`` names the PRs of a post's stack to cover, each with who
        is asked about it (``None``: the ones that are ready). ``reassign``
        drafts moving the posted PR to another reviewer."""
        if reassign:
            return self._draft_reassign(repo_id, branch, assignee_id)
        kind = self._next_kind(repo_id, branch)
        if kind == "post_slack":
            return self._draft_post(repo_id, branch, assignee_id, include)
        if kind == "bump_slack":
            return self._draft_bump(repo_id, branch, assignee_id)
        raise SlackNotApplicable(
            "Nothing to post or bump: the branch's next action is not about Slack."
        )

    def _draft_post(
        self,
        repo_id: int,
        branch: str,
        assignee_id: str | None,
        include: list[api.SlackPrRef] | None,
    ) -> api.SlackDraft:
        parents = self._parents(repo_id)
        above: list[str] = []
        current = branch
        while parents.get(current) in parents:
            current = parents[current]
            above.append(current)
        order = [*reversed(above)]
        order += descendants(parents, branch)
        ready: set[str] = set()
        candidates: list[_Pr] = []
        for name in order:
            action = self._next_action(repo_id, name)
            if action is None or (pr := self._pr(repo_id, name)) is None:
                continue
            if action.kind == "post_slack":
                ready.add(name)
            elif not _held_post(action):
                continue
            candidates.append(pr)
        if include is None:
            wanted: dict[tuple[str, int], str | None] = {
                (pr.repo, pr.number): None for pr in candidates if pr.branch in ready
            }
        else:
            wanted = {(ref.repo, ref.number): ref.assignee_id for ref in include}
        prs = [pr for pr in candidates if (pr.repo, pr.number) in wanted]
        if not prs:
            raise SlackNotApplicable("Pick at least one PR to post.")
        numbers = {pr.branch: pr.number for pr in prs}
        history = self._posts()
        lines = sum(pr.additions + pr.deletions for pr in prs)
        chosen = self._person(assignee_id) or choose_assignee(
            self._config, repo=prs[0].repo, lines=lines, history=history
        )
        asked = {
            key: self._person(person_id) or chosen for key, person_id in wanted.items()
        }
        text = compose_post(
            [
                PostPr(
                    number=pr.number,
                    url=pr.url,
                    title=pr.title,
                    additions=pr.additions,
                    deletions=pr.deletions,
                    assignee=asked[(pr.repo, pr.number)],
                    parent_number=numbers.get(parents.get(pr.branch, "")),
                )
                for pr in prs
            ]
        )
        return self._draft_of(
            "post",
            text,
            chosen,
            candidates,
            None,
            included={(pr.repo, pr.number) for pr in prs},
            asked=asked,
        )

    def _draft_bump(
        self, repo_id: int, branch: str, assignee_id: str | None
    ) -> api.SlackDraft:
        pr = self._pr(repo_id, branch)
        assert pr is not None
        history = self._posts()
        last = _last_post(history, pr)
        assert last is not None
        self._require_configured_channel(last, pr)
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
        text = compose_bump(
            [BumpPr(p.repo, p.number, p.url, p.additions, p.deletions) for p in due],
            chosen,
        )
        return self._draft_of(
            "bump",
            text,
            chosen,
            due,
            last.thread_ts,
            included={(p.repo, p.number) for p in due},
        )

    def _draft_reassign(
        self, repo_id: int, branch: str, assignee_id: str | None
    ) -> api.SlackDraft:
        """Asks someone else about the posted PR, in its thread, at any time
        while the PR is open: not only once its bump is due."""
        pr = self._pr(repo_id, branch)
        fetched = self._fetched(repo_id, branch)
        if pr is None:
            raise SlackNotApplicable("The branch has no PR to reassign.")
        assert fetched is not None and fetched.status is not None
        if fetched.status.pr not in (PrState.OPEN, PrState.DRAFT):
            raise SlackNotApplicable(f"{pr.repo}#{pr.number} is not open.")
        history = self._posts()
        last = _last_post(history, pr)
        if last is None:
            raise SlackNotApplicable(
                f"{pr.repo}#{pr.number} is not posted in Slack: post it first."
            )
        self._require_configured_channel(last, pr)
        asked = frozenset(
            row.assignee_name
            for row in history
            if (row.repo, row.pr_number) == (pr.repo, pr.number) and row.assignee_name
        )
        chosen = self._person(assignee_id) or choose_assignee(
            self._config,
            repo=pr.repo,
            lines=pr.additions + pr.deletions,
            history=history,
            bump=True,
            exclude=asked,
        )
        text = compose_reassign(
            BumpPr(pr.repo, pr.number, pr.url, pr.additions, pr.deletions),
            chosen,
        )
        return self._draft_of(
            "reassign",
            text,
            chosen,
            [pr],
            last.thread_ts,
            included={(pr.repo, pr.number)},
        )

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
        *,
        included: set[tuple[str, int]],
        asked: dict[tuple[str, int], Person | None] | None = None,
    ) -> api.SlackDraft:
        """``prs`` is every PR the dialog lists, ``included`` the ones the text
        is about and ``asked`` who each of them asks about (else ``chosen``)."""
        asked = asked or {}
        return api.SlackDraft(
            kind=kind,  # type: ignore[arg-type]
            channel=self._config.channel,
            text=text,
            assignee=_person_model(chosen),
            candidates=[
                api.SlackPerson(id=p.id, name=p.name) for p in self._config.reviewers
            ],
            prs=[
                self._draft_pr(
                    pr,
                    (pr.repo, pr.number) in included,
                    asked.get((pr.repo, pr.number), chosen),
                )
                for pr in prs
            ],
            thread_ts=thread_ts,
            thread_link=(
                self._config.permalink(self._config.channel, thread_ts)
                if thread_ts
                else None
            ),
        )

    def _draft_pr(
        self, pr: _Pr, included: bool, asked: Person | None
    ) -> api.SlackDraftPr:
        fetched = self._fetched(pr.repo_id, pr.branch)
        assert fetched is not None and fetched.status is not None
        status = fetched.status
        return api.SlackDraftPr(
            repo=pr.repo,
            number=pr.number,
            url=pr.url,
            title=pr.title,
            additions=pr.additions,
            deletions=pr.deletions,
            assignee=_person_model(asked),
            included=included,
            pr=status.pr,
            ci=status.ci,
            review=status.review,
            open_comments=status.open_comments,
            has_conflicts=status.has_conflicts,
            next=self._next_action(pr.repo_id, pr.branch),
        )

    def _require_configured_channel(self, last: SlackPostRow, pr: _Pr) -> None:
        """A bump replies in the thread of the last post, which must be in the
        configured channel: a thread elsewhere (an imported post in another
        channel) is never replied to."""
        if last.channel != self._config.channel:
            raise SlackNotApplicable(
                f"{pr.repo}#{pr.number} was last posted in {last.channel}, not in "
                f"the configured channel {self._config.channel}."
            )

    def _record(self, session_id: str | None, purpose: str, cost_usd: float) -> None:
        """Records what a session cost under its id (a session with no id stays
        in the interactive spend, which counts its calls like any other's)."""
        if session_id:
            self._record_session(
                SlackSessionRow(session_id, self._wall_clock(), purpose, cost_usd)
            )

    # ---- sending ---------------------------------------------------------------

    async def send(self, request: api.SlackSendRequest) -> api.SlackAccepted:
        """Checks the message can go, then posts it in the background: the
        Slack session takes tens of seconds, so this returns at once and the
        PRs read ``posting`` (see ``SlackTracker``) until it is done. Raises
        when it cannot go (already posted or being posted, not that thread)."""
        prs = [self._pr_of(ref) for ref in request.prs]
        if not prs:
            raise SlackNotApplicable("The message names no PR.")
        history = self._posts()
        for pr in prs:
            last = _last_post(history, pr)
            if self._tracker.is_posting(pr.repo, pr.number):
                raise SlackNotApplicable(
                    f"{pr.repo}#{pr.number} is already being posted in Slack."
                )
            if request.kind == "post" and last is not None:
                raise SlackNotApplicable(
                    f"{pr.repo}#{pr.number} is already posted in Slack."
                )
            if request.kind != "post" and (
                last is None or last.thread_ts != request.thread_ts
            ):
                raise SlackNotApplicable(
                    f"{pr.repo}#{pr.number} was not posted in that thread."
                )
            if request.kind != "post" and last is not None:
                self._require_configured_channel(last, pr)
        if request.kind == "reassign" and len(prs) != 1:
            raise SlackNotApplicable("A reassignment is about one PR.")
        keys = [(pr.repo, pr.number) for pr in prs]
        branches = {(pr.repo_id, pr.branch) for pr in prs}
        self._tracker.begin_posting(keys)
        self._changed(branches)
        task = asyncio.create_task(self._post(request, prs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return api.SlackAccepted(prs=request.prs)

    async def idle(self) -> None:
        """Returns when no post is in flight."""
        while self._tasks:
            await asyncio.gather(*self._tasks)

    async def _post(self, request: api.SlackSendRequest, prs: list[_Pr]) -> None:
        keys = [(pr.repo, pr.number) for pr in prs]
        branches = {(pr.repo_id, pr.branch) for pr in prs}
        notice = None
        try:
            notice = await self._post_and_record(request, prs)
        except Exception as error:  # a failed post says why, and records nothing
            LOGGER.exception("posting in Slack failed")
            notice = f"Posting in Slack failed: {error}"
        finally:
            self._tracker.end_posting(keys, notice)
            self._changed(branches)

    async def _post_and_record(
        self, request: api.SlackSendRequest, prs: list[_Pr]
    ) -> str | None:
        """Posts and records it; returns what Slack's copy lacked, if anything."""
        mentions = {p.name: p.id for p in self._config.reviewers}
        text = to_slack(request.text, mentions)
        sent = await self._send(
            channel=self._config.channel,
            text=text,
            thread_ts=request.thread_ts if request.kind != "post" else None,
        )
        self._record(sent.session_id, request.kind, sent.cost_usd)
        asked = {(ref.repo, ref.number): ref.assignee_id for ref in request.prs}

        def person_for(pr: _Pr) -> Person | None:
            """Who was asked about this PR: its own assignee, else the message's."""
            return self._person(asked.get((pr.repo, pr.number)) or request.assignee_id)

        self._save(
            [
                SlackPostRow(
                    channel=self._config.channel,
                    ts=sent.ts,
                    thread_ts=sent.thread_ts,
                    repo=pr.repo,
                    pr_number=pr.number,
                    kind=request.kind,
                    posted_at=self._wall_clock(),
                    assignee_id=asked_of.id if asked_of else None,
                    assignee_name=asked_of.name if asked_of else None,
                )
                for pr in prs
                for asked_of in [person_for(pr)]
            ]
        )
        self._tracker.reload()
        return _verify(text, sent)


def _held_post(action: api.NextActionInfo) -> bool:
    """An agent holds the task, and the post is what it would be doing next
    if it did not."""
    return (
        action.kind in ("claimed", "autonomous")
        and action.would_be is not None
        and action.would_be.kind == "post_slack"
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
