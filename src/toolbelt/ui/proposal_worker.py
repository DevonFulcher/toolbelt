"""Picks up approved proposals: one worker run at a time.

For the oldest approved proposal nobody has taken up it finds the repo, makes
sure there is a Jira ticket, makes a stacked branch with its worktree and starts
an implement run there (``AgentRunner``). The run stops at a committed local
branch with no PR: the branch then goes through the usual Validate and personal
review. A proposal that cannot be taken up gets a note saying why and is left
alone (the Proposals view shows it); one that only has to wait (the repo's
checkout is busy) is tried again on a later tick.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from toolbelt.autonomous_agents.dispatcher import Decision
from toolbelt.autonomous_agents.eligibility import Reason
from toolbelt.autonomous_agents.implement_setup import (
    SetupFailed,
    brief_of,
    is_task_key,
)
from toolbelt.autonomous_agents.limits import IMPLEMENT
from toolbelt.git.stack.store import StackStore
from toolbelt.logger import logger
from toolbelt.proposals.book import ProposalBook
from toolbelt.proposals.model import Proposal
from toolbelt.proposals.settings import ProposalSettings

DEFAULT_TICK_SECONDS = 20.0


@dataclass(frozen=True)
class RepoRef:
    id: int
    name: str
    #: ``owner/name`` of its GitHub remote, when it has one.
    slug: str | None
    path: Path


class ProposalWorker:
    def __init__(
        self,
        *,
        book: ProposalBook,
        db_path: Callable[[], Path],
        settings: ProposalSettings,
        repos: Callable[[], list[RepoRef]],
        blockers: Callable[[], list[Reason]],
        launching: Callable[[], bool],
        checkout_problem: Callable[[Path], str | None],
        create_ticket: Callable[[Proposal, str], Awaitable[str]],
        append_branch: Callable[[Path, Proposal, str], tuple[Path, str]],
        head_sha: Callable[[Path], str],
        start: Callable[[Decision, str], Awaitable[str]],
        wall_clock: Callable[[], datetime],
        changed: Callable[[], None] = lambda: None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._book = book
        self._db_path = db_path
        self._settings = settings
        self._repos = repos
        self._blockers = blockers
        self._launching = launching
        self._checkout_problem = checkout_problem
        self._create_ticket = create_ticket
        self._append_branch = append_branch
        self._head_sha = head_sha
        self._start = start
        self._wall_clock = wall_clock
        self._changed = changed
        self._sleep = sleep
        self._tick = tick

    def _working(self) -> bool:
        store = StackStore(self._db_path())
        try:
            return any(
                r.trigger == IMPLEMENT and r.state == "running"
                for r in store.agent_runs()
            )
        finally:
            store.close()

    def _next(self) -> Proposal | None:
        return next(
            (
                p
                for p in self._book.all()
                if p.status == "approved"
                and p.worker_run_id is None
                and not p.work_note
            ),
            None,
        )

    def _repo_of(self, proposal: Proposal) -> RepoRef | None:
        wanted = proposal.content.repo.lower()
        return next(
            (
                r
                for r in self._repos()
                if wanted in (r.name.lower(), (r.slug or "").lower())
            ),
            None,
        )

    def _fail(self, proposal: Proposal, note: str) -> None:
        logger.warning("proposal %s cannot be taken up: %s", proposal.id, note)
        self._book.record_work(proposal.id, work_note=note)
        self._changed()

    async def step(self) -> None:
        """Take up the next approved proposal, if the agents may start a run."""
        if self._blockers() or not self._launching():
            return
        proposal = await asyncio.to_thread(self._next)
        if proposal is None or await asyncio.to_thread(self._working):
            return
        repo = self._repo_of(proposal)
        if repo is None:
            self._fail(proposal, f"no tracked repo matches {proposal.content.repo!r}")
            return
        if proposal.branch is not None:
            self._fail(proposal, f"branch {proposal.branch} exists but no run started")
            return
        if await asyncio.to_thread(self._checkout_problem, repo.path) is not None:
            return  # busy: tried again on a later tick
        try:
            task_key = await self._task_key(proposal)
            self._book.record_work(proposal.id, task_key=task_key)
            worktree, branch = await asyncio.to_thread(
                self._append_branch, repo.path, proposal, task_key
            )
            self._book.record_work(proposal.id, branch=branch)
            decision = await asyncio.to_thread(
                self._decision, repo, proposal, worktree, branch
            )
            run_id = await self._start(decision, proposal.id)
        except SetupFailed as failure:
            self._fail(proposal, str(failure))
            return
        self._book.record_work(proposal.id, worker_run_id=run_id)
        self._changed()

    async def _task_key(self, proposal: Proposal) -> str:
        if proposal.task_key:
            return proposal.task_key
        if proposal.source == "jira" and is_task_key(proposal.source_ref):
            return proposal.source_ref
        if self._settings.jira_project is None:
            raise SetupFailed(
                "it did not come from Jira, and proposals.jira_project is not "
                "set, so no ticket can be created for it"
            )
        return await self._create_ticket(proposal, self._settings.jira_project)

    def _decision(
        self, repo: RepoRef, proposal: Proposal, worktree: Path, branch: str
    ) -> Decision:
        store = StackStore(self._db_path())
        try:
            task = next(
                (
                    t
                    for t in store.tracked_branch_facts()
                    if t.repo_id == repo.id and t.name == branch
                ),
                None,
            )
            base = store.get_parent(repo.id, branch)
        finally:
            store.close()
        if task is None:
            raise SetupFailed(f"branch {branch} is not tracked after it was made")
        return Decision(
            task_id=task.task_id,
            repo_id=repo.id,
            repo=repo.name,
            branch=branch,
            trigger=IMPLEMENT,
            reason=None,
            head_sha=self._head_sha(worktree),
            decided_at=self._wall_clock(),
            base_ref=base,
            worktree=worktree,
            brief=brief_of(proposal),
        )

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except Exception:  # noqa: BLE001 - one bad tick must not end the worker
                logger.exception("the proposal worker failed")
            await self._sleep(self._tick)
