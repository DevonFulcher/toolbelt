"""The views that are pushed to the page instead of asked for: what the task
cards show beyond their PR status, the Agents view, the Spend view and the
conversation of a run that is working.

Each is worked out in the background when something that changes it happens
(the agents decided, a run started or ended, the transcripts were read, the
stack changed, a switch was flipped), kept in memory for the snapshot, and
published as an event when it differs from the last one. Nothing here polls an
API: what polls is the sources (GitHub, the transcripts), and the page hears of
each change as it is found.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from toolbelt.autonomous_agents.conversation import find_transcript, read_conversation
from toolbelt.autonomous_agents.dispatcher import Dispatcher
from toolbelt.autonomous_agents.runner import AgentRunner
from toolbelt.finish_estimate.model import Prior
from toolbelt.finish_estimate.working_hours import WorkingHours
from toolbelt.git.stack.store import StackStore
from toolbelt.ui import api_models as api
from toolbelt.ui.agents_view import build_agents
from toolbelt.ui.card_extras import build_card_extras
from toolbelt.ui.estimate_feed import ModelHub
from toolbelt.ui.live_status import EventHub
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.spend_feed import BREAKDOWN_DAYS, SpendFeed

#: How often the transcripts of the runs that are working are looked at.
CONVERSATION_TICK_SECONDS = 1.0


def conversation_of(
    run_id: str, *, db_path: Path, projects: Path
) -> api.AgentConversation | None:
    """What a run said and did, from its session transcript (``None``: no such
    run). Blocking."""
    store = StackStore(db_path)
    try:
        run = next((r for r in store.agent_runs() if r.run_id == run_id), None)
    finally:
        store.close()
    if run is None:
        return None
    transcript = find_transcript(run.session_id, projects) if run.session_id else None
    entries = read_conversation(transcript) if transcript else []
    return api.AgentConversation(
        run_id=run_id,
        running=run.state == "running",
        messages=[
            api.AgentMessage(
                kind=e.kind, at=e.at, text=e.text, tool=e.tool, is_error=e.is_error
            )
            for e in entries
        ],
    )


class PushedViews:
    def __init__(
        self,
        *,
        hub: EventHub,
        dispatcher: Dispatcher,
        runner: AgentRunner,
        spend: SpendFeed,
        db_path: Callable[[], Path],
        projects: Callable[[], Path],
        enabled: Callable[[], bool],
        launching: Callable[[], bool],
        wall_clock: Callable[[], datetime],
        conversation_tick: float = CONVERSATION_TICK_SECONDS,
        repo_slug: Callable[[int], str | None] = lambda repo_id: None,
        pr_facts: Callable[[int, str], tuple[PrFacts, str] | None] = lambda *_: None,
        opening_pr: Callable[[int, str], tuple[bool, str | None]] = lambda *_: (
            False,
            None,
        ),
        working_hours: Callable[[], WorkingHours] = WorkingHours,
        estimate_hub: ModelHub | None = None,
        priors: Callable[[], dict[str, Prior]] = dict,
        build_proposals: Callable[[], api.ProposalList] | None = None,
    ) -> None:
        self._build_proposals = build_proposals
        self._estimate_hub = estimate_hub
        self._priors = priors
        #: Every model's answer for each task in flight, by card id: what the
        #: estimate popup reads (worked out with the card extras).
        self.breakdowns: dict[str, api.EstimateBreakdown] = {}
        self._opening_pr = opening_pr
        self._working_hours = working_hours
        self._repo_slug = repo_slug
        self._pr_facts = pr_facts
        self._hub = hub
        self._dispatcher = dispatcher
        self._runner = runner
        self._spend = spend
        self._db_path = db_path
        self._projects = projects
        self._enabled = enabled
        self._launching = launching
        self._wall_clock = wall_clock
        self._conversation_tick = conversation_tick
        self.extras: api.CardExtras | None = None
        self.agents: api.Agents | None = None
        self.proposals: api.ProposalList | None = None
        self.spend: list[api.Spend] = []
        self._last: dict[str, str] = {}
        self._running: set[str] = set()
        self._again: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._conversations: dict[str, str] = {}

    # ---- working the views out ----

    def _compute_extras(self) -> api.CardExtras:
        store = StackStore(self._db_path())
        try:
            return build_card_extras(
                self._dispatcher,
                store,
                self._wall_clock(),
                self._spend.ingested_at,
                self._repo_slug,
                self._pr_facts,
                self._opening_pr,
                self._working_hours(),
                self._estimate_hub,
                self._priors(),
                self.breakdowns,
            )
        finally:
            store.close()

    def _compute_agents(self) -> api.Agents:
        store = StackStore(self._db_path())
        try:
            return build_agents(
                self._dispatcher,
                store,
                enabled=self._enabled(),
                launching=self._launching(),
                now=self._wall_clock(),
            )
        finally:
            store.close()

    def _compute_proposals(self) -> api.ProposalList:
        assert self._build_proposals is not None
        return self._build_proposals()

    def _compute_spend(self) -> list[api.Spend]:
        return [self._spend.spend(days) for days in BREAKDOWN_DAYS]

    async def _refresh(self, kind: str) -> None:
        compute = {
            "extras": self._compute_extras,
            "agents": self._compute_agents,
            "proposals": self._compute_proposals,
            "spend": self._compute_spend,
        }[kind]
        value = await asyncio.to_thread(compute)
        if kind == "spend":
            assert isinstance(value, list)
            text = "[" + ",".join(item.model_dump_json() for item in value) + "]"
        else:
            assert isinstance(value, BaseModel)
            text = value.model_dump_json()
        setattr(self, kind, value)
        if text == self._last.get(kind):
            return
        self._last[kind] = text
        payload = (
            [item.model_dump(mode="json") for item in value]
            if isinstance(value, list)
            else value.model_dump(mode="json")
        )
        self._hub.publish(kind, {kind: payload})

    async def _run(self, kind: str) -> None:
        try:
            while True:
                self._again.discard(kind)
                await self._refresh(kind)
                if kind not in self._again:
                    return
        finally:
            self._running.discard(kind)

    def notify(self, *kinds: str) -> None:
        """Something changed that ``kinds`` (``extras``, ``agents``, ``spend``)
        show: work them out again, off the event loop, once at a time each. A
        change that arrives meanwhile is picked up by a second pass. Outside
        the event loop (nothing is serving) there is nobody to tell."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for kind in kinds:
            if kind in self._running:
                self._again.add(kind)
                continue
            self._running.add(kind)
            task = loop.create_task(self._run(kind))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def everything_changed(self) -> None:
        self.notify(
            "extras",
            "agents",
            "spend",
            *(("proposals",) if self._build_proposals else ()),
        )

    # ---- conversations of runs that are working ----

    async def _push_conversation(self, run_id: str) -> None:
        conversation = await asyncio.to_thread(
            conversation_of,
            run_id,
            db_path=self._db_path(),
            projects=self._projects(),
        )
        if conversation is None:
            return
        text = conversation.model_dump_json()
        if text == self._conversations.get(run_id):
            return
        self._conversations[run_id] = text
        self._hub.publish(
            "conversation", {"conversation": conversation.model_dump(mode="json")}
        )

    async def watch_conversations(self) -> None:
        """While runs are working, publish each one's conversation as its
        transcript grows, and once more when it ends."""
        watching: set[str] = set()
        while True:
            working = set(self._runner.running)
            for run_id in sorted(working | watching):
                await self._push_conversation(run_id)
            for run_id in watching - working:
                self._conversations.pop(run_id, None)
            watching = working
            await asyncio.sleep(self._conversation_tick)

    async def run(self) -> None:
        self.everything_changed()
        await self.watch_conversations()
