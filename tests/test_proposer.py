"""The proposer: when it may look (``proposer_wait``), one run of it, and the
feed that keeps the pending proposals topped up."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage
from test_proposals import content_data

from toolbelt.autonomous_agents.eligibility import Reason
from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.autonomous_agents.proposer import ProposerRunner
from toolbelt.autonomous_agents.proposer_eligibility import proposer_wait
from toolbelt.git.stack.store import AgentRun, StackStore, TokenCounts
from toolbelt.proposals.book import ProposalBook
from toolbelt.proposals.settings import ProposalSettings
from toolbelt.ui.proposal_feed import ProposalFeed

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SETTINGS = ProposalSettings()


def last_run(
    *, started: timedelta, state: str = "succeeded", slots: int = 3
) -> AgentRun:
    return AgentRun(
        run_id="r1",
        task_id="proposals",
        repo_id=0,
        branch="",
        trigger="propose",
        head_sha="",
        model="m",
        state=state,
        started_at=NOW - started,
        ended_at=NOW - started + timedelta(minutes=5),
        session_id=None,
        turns=1,
        cost_usd=0.1,
        tokens=TokenCounts(),
        summary=None,
        error=None,
        slots=slots,
    )


def wait(**changes):
    base = {
        "now": NOW,
        "settings": SETTINGS,
        "pending": 2,
        "running": False,
        "last_run": None,
        "last_stored": 0,
        "last_decided_at": None,
    }
    return proposer_wait(**{**base, **changes})


def test_it_does_not_look_while_the_cap_is_reached():
    result = wait(pending=5)

    assert result is not None and "5 proposals are waiting" in result.text


def test_it_looks_when_it_never_has():
    assert wait() is None


def test_it_does_not_start_a_second_look():
    result = wait(running=True)

    assert result is not None and result.text == "Looking for proposals…"


def test_it_looks_again_once_the_cadence_has_passed():
    assert wait(last_run=last_run(started=timedelta(hours=5)), last_stored=3) is None


def test_a_decision_makes_it_look_again_after_the_settle_delay():
    run = last_run(started=timedelta(hours=1))

    assert (
        wait(last_run=run, last_stored=3, last_decided_at=NOW - timedelta(minutes=3))
        is None
    )


def test_a_decision_just_made_waits_for_the_settle_delay():
    run = last_run(started=timedelta(hours=1))
    decided = NOW - timedelta(seconds=30)

    result = wait(last_run=run, last_stored=3, last_decided_at=decided)

    assert result is not None
    assert result.until == decided + timedelta(seconds=SETTINGS.settle_seconds)


def test_a_look_that_came_back_short_waits_for_the_cadence_even_after_a_decision():
    run = last_run(started=timedelta(hours=1), slots=3)

    result = wait(
        last_run=run, last_stored=1, last_decided_at=NOW - timedelta(minutes=10)
    )

    assert result is not None
    assert result.until == run.started_at + timedelta(hours=SETTINGS.cadence_hours)


def test_a_failed_look_waits_for_the_cadence():
    run = last_run(started=timedelta(hours=1), state="failed")

    result = wait(last_run=run, last_decided_at=NOW - timedelta(minutes=10))

    assert result is not None and "failed" in result.text


def structured(count: int) -> dict:
    return {
        "proposals": [
            {
                **content_data(f"t{n}"),
                "source": "jira",
                "source_ref": f"TB-{n}",
                "source_url": f"https://j/{n}",
            }
            for n in range(count)
        ],
        "sources_read": ["jira"],
    }


def result_message(output) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=4,
        session_id="s",
        total_cost_usd=0.2,
        usage={"input_tokens": 1, "output_tokens": 1},
        result="",
        structured_output=output,
    )


class Rig:
    def __init__(self, tmp_path: Path, output=None, *, raises=None):
        self.db = tmp_path / "stacks.db"
        self.calls: list[dict] = []

        async def query(*, prompt: str, options) -> AsyncIterator:
            self.calls.append({"prompt": prompt, "options": options})
            if raises:
                raise raises
            yield AssistantMessage(content=[], model="m", usage={}, message_id="m1")
            yield result_message(output)

        self.book = ProposalBook(
            open_store=lambda: StackStore(self.db), wall_clock=lambda: NOW
        )
        self.runner = ProposerRunner(
            db_path=lambda: self.db,
            workdir=lambda: tmp_path / "proposer",
            limits=AgentLimits(),
            book=self.book,
            repos=lambda: ["o/toolbelt"],
            history=self.book.all,
            wall_clock=lambda: NOW,
            query=query,
        )

    async def run(self, slots: int) -> None:
        await self.runner.start(slots)
        assert self.runner.running is not None
        await self.runner.running

    def only_run(self) -> AgentRun:
        store = StackStore(self.db)
        try:
            (found,) = store.agent_runs()
        finally:
            store.close()
        return found


def test_a_run_stores_what_it_found_and_records_itself(tmp_path):
    rig = Rig(tmp_path, structured(2))

    asyncio.run(rig.run(3))

    assert [p.content.title for p in rig.book.all()] == ["t0", "t1"]
    run = rig.only_run()
    assert (run.trigger, run.state, run.slots, run.task_id) == (
        "propose",
        "succeeded",
        3,
        "proposals",
    )
    assert run.summary == "Proposed 2 of 3: t0; t1"
    assert run.cost_usd == 0.2


def test_a_run_is_given_the_slots_the_repos_and_the_user_settings(tmp_path):
    rig = Rig(tmp_path, structured(0))

    asyncio.run(rig.run(3))

    options = rig.calls[0]["options"]
    assert options.setting_sources == ["user"]
    assert "Bash" in options.disallowed_tools
    assert "at most 3 items" in options.system_prompt["append"]
    assert "o/toolbelt" in options.system_prompt["append"]


def test_more_than_the_free_slots_is_trimmed_to_the_cap(tmp_path):
    rig = Rig(tmp_path, structured(8))

    asyncio.run(rig.run(5))

    assert len(rig.book.pending()) == 5


def test_a_proposal_that_does_not_fit_the_shape_does_not_lose_the_rest(tmp_path):
    output = structured(2)
    del output["proposals"][0]["why_now"]
    rig = Rig(tmp_path, output)

    asyncio.run(rig.run(3))

    assert [p.content.title for p in rig.book.all()] == ["t1"]


def test_a_failed_run_is_recorded_with_its_error(tmp_path):
    rig = Rig(tmp_path, raises=RuntimeError("boom"))

    asyncio.run(rig.run(3))

    run = rig.only_run()
    assert run.state == "failed" and "boom" in (run.error or "")
    assert rig.book.all() == []


class FakeRunner:
    def __init__(self):
        self.running = None
        self.started: list[int] = []

    async def start(self, slots: int) -> str:
        self.started.append(slots)
        return "r"


def feed(tmp_path, *, blockers=(), launching=True):
    statuses: list[str] = []
    book = ProposalBook(
        open_store=lambda: StackStore(tmp_path / "stacks.db"), wall_clock=lambda: NOW
    )
    runner = FakeRunner()
    proposal_feed = ProposalFeed(
        book=book,
        runner=runner,  # type: ignore[arg-type]
        db_path=lambda: tmp_path / "stacks.db",
        settings=SETTINGS,
        blockers=lambda: list(blockers),
        launching=lambda: launching,
        set_status=statuses.append,
        wall_clock=lambda: NOW,
    )
    return proposal_feed, runner, statuses


def test_the_feed_starts_one_run_for_every_free_slot(tmp_path):
    proposal_feed, runner, statuses = feed(tmp_path)

    asyncio.run(proposal_feed.step())

    assert runner.started == [5]
    assert statuses == ["Looking for proposals…"]


def test_the_feed_says_why_when_the_agents_are_blocked(tmp_path):
    blocked = [Reason("disabled", "Autonomous agents are switched off.")]
    proposal_feed, runner, statuses = feed(tmp_path, blockers=blocked)

    asyncio.run(proposal_feed.step())

    assert runner.started == []
    assert statuses == ["Autonomous agents are switched off."]


def test_the_feed_starts_nothing_in_a_dry_run(tmp_path):
    proposal_feed, runner, statuses = feed(tmp_path, launching=False)

    asyncio.run(proposal_feed.step())

    assert runner.started == []
    assert "not set to start runs" in statuses[0]


def test_guidelines_are_versioned_and_never_overwritten(tmp_path):
    book = ProposalBook(
        open_store=lambda: StackStore(tmp_path / "stacks.db"), wall_clock=lambda: NOW
    )

    book.write_guidelines("- small fixes", author="curator")
    book.write_guidelines("- small fixes\n- no migrations", author="user")

    latest = book.guidelines()
    assert latest is not None and latest.author == "user"
    store = StackStore(tmp_path / "stacks.db")
    try:
        count = store._conn.execute(
            "SELECT COUNT(*) AS n FROM proposal_guidelines"
        ).fetchone()["n"]
    finally:
        store.close()
    assert count == 2


def test_the_curator_writes_guidelines_from_the_decisions(tmp_path):
    from toolbelt.autonomous_agents.curator import CuratorRunner

    rig = Rig(tmp_path, structured(0))
    (stored,) = rig.book.propose(
        [
            (
                "jira",
                "TB-1",
                "https://j/1",
                __import__(
                    "toolbelt.proposals.model", fromlist=["x"]
                ).content_from_data(content_data()),
            )
        ],
        run_id=None,
    )
    rig.book.reject(stored.id, reason="too big", tags=["too_big"])
    seen: dict = {}

    async def query(*, prompt: str, options) -> AsyncIterator:
        seen["system"] = options.system_prompt
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.01,
            usage={"input_tokens": 1, "output_tokens": 1},
            result="- Prefer small fixes",
        )

    curator = CuratorRunner(
        db_path=lambda: rig.db,
        workdir=lambda: tmp_path / "proposer",
        limits=AgentLimits(),
        book=rig.book,
        wall_clock=lambda: NOW,
        query=query,
    )

    async def go():
        await curator.start()
        assert curator.running is not None
        await curator.running

    asyncio.run(go())

    assert "too_big" in seen["system"]
    latest = rig.book.guidelines()
    assert latest is not None
    assert (latest.body, latest.author, latest.decisions_seen) == (
        "- Prefer small fixes",
        "curator",
        1,
    )
