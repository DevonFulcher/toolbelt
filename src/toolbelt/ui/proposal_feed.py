"""Keeps the pending proposals topped up to the cap.

Every tick it decides whether the proposer agent should look for proposals now
(``proposer_wait``, after the agents' shared blockers: the switches, the spend
limits, sign-in and the screen) and starts one run for all the free slots when
it should. Deciding a proposal frees a slot, so the proposer is started again
shortly after, without waiting for the cadence. What it is doing, or why not,
is the sentence the Proposals view shows.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from toolbelt.autonomous_agents.curator import CuratorRunner
from toolbelt.autonomous_agents.eligibility import Reason
from toolbelt.autonomous_agents.proposer import PROPOSE, ProposerRunner
from toolbelt.autonomous_agents.proposer_eligibility import (
    ProposerWait,
    proposer_wait,
)
from toolbelt.git.stack.store import StackStore
from toolbelt.logger import logger
from toolbelt.proposals.book import ProposalBook
from toolbelt.proposals.settings import ProposalSettings

DEFAULT_TICK_SECONDS = 30.0


def _wait_text(wait: ProposerWait) -> str:
    if wait.until is None:
        return wait.text
    return f"{wait.text} Next look about {wait.until.astimezone():%-I:%M %p}."


class ProposalFeed:
    def __init__(
        self,
        *,
        book: ProposalBook,
        runner: ProposerRunner,
        db_path: Callable[[], Path],
        settings: ProposalSettings,
        blockers: Callable[[], list[Reason]],
        launching: Callable[[], bool],
        set_status: Callable[[str], None],
        wall_clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = DEFAULT_TICK_SECONDS,
        curator: CuratorRunner | None = None,
    ) -> None:
        self._curator = curator
        self._book = book
        self._runner = runner
        self._db_path = db_path
        self._settings = settings
        self._blockers = blockers
        self._launching = launching
        self._set_status = set_status
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._tick = tick

    def _look(self) -> tuple[ProposerWait | None, int]:
        """Whether to start now, and how many slots a run would fill."""
        store = StackStore(self._db_path())
        try:
            runs = [r for r in store.agent_runs() if r.trigger == PROPOSE]
            proposals = store.proposals()
        finally:
            store.close()
        pending = sum(1 for p in proposals if p.status == "pending")
        last_run = runs[0] if runs else None
        decided = [p.decided_at for p in proposals if p.decided_at]
        wait = proposer_wait(
            now=self._wall_clock(),
            settings=self._settings,
            pending=pending,
            running=self._runner.running is not None,
            last_run=last_run,
            last_stored=sum(
                1 for p in proposals if last_run and p.run_id == last_run.run_id
            ),
            last_decided_at=datetime.fromisoformat(max(decided)) if decided else None,
        )
        return wait, self._settings.max_pending - pending

    def _curation_due(self) -> bool:
        """Enough proposals were decided since the guidelines were last
        written for another look at them."""
        if self._curator is None or self._curator.running is not None:
            return False
        guidelines = self._book.guidelines()
        seen = guidelines.decisions_seen if guidelines else 0
        return (
            self._book.decided_count() - seen >= self._settings.curate_after_decisions
        )

    async def step(self) -> None:
        """Decide once, and start the proposer when it should look."""
        blockers = self._blockers()
        if blockers:
            self._set_status(blockers[0].text)
            return
        if not self._launching():
            self._set_status(
                "Agents are not set to start runs: not looking for proposals."
            )
            return
        if self._curator is not None and await asyncio.to_thread(self._curation_due):
            await self._curator.start()
        wait, slots = await asyncio.to_thread(self._look)
        if wait is not None:
            self._set_status(_wait_text(wait))
            return
        self._set_status("Looking for proposals…")
        await self._runner.start(slots)

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except Exception:  # noqa: BLE001 - one bad tick must not end the feed
                logger.exception("the proposal feed failed")
            await self._sleep(self._tick)
