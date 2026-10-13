"""The agent run records in the stack store."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolbelt.git.stack.store import StackStore, TokenCounts

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def start(store: StackStore, *, task_id="t1", at=T0, trigger="fix_ci") -> str:
    return store.start_agent_run(
        task_id=task_id,
        repo_id=1,
        branch="devon/a",
        trigger=trigger,
        head_sha="abc123",
        model="claude-haiku-5-5",
        at=at,
    )


def test_a_started_run_is_running_with_nothing_spent(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")

    run_id = start(store)

    (run,) = store.agent_runs()
    assert (run.run_id, run.state, run.ended_at) == (run_id, "running", None)
    assert (run.task_id, run.trigger, run.head_sha) == ("t1", "fix_ci", "abc123")
    assert (run.turns, run.cost_usd, run.tokens) == (0, 0.0, TokenCounts())


def test_finishing_a_run_records_what_it_spent_and_said(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    run_id = start(store)

    store.finish_agent_run(
        run_id,
        state="succeeded",
        ended_at=T0 + timedelta(minutes=4),
        session_id="sdk-session",
        turns=12,
        cost_usd=0.0831,
        tokens=TokenCounts(
            input=3, output=900, cache_read=70_000, cache_write_1h=5_000
        ),
        summary="Rebased and resolved the lockfile.",
    )

    (run,) = store.agent_runs()
    assert (run.state, run.session_id, run.turns) == ("succeeded", "sdk-session", 12)
    assert run.ended_at == T0 + timedelta(minutes=4)
    assert run.cost_usd == pytest.approx(0.0831)
    assert run.tokens.cache_read == 70_000
    assert run.summary == "Rebased and resolved the lockfile."


def test_a_run_that_already_ended_is_left_as_it_is(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    run_id = start(store)
    store.finish_agent_run(run_id, state="stopped", ended_at=T0, cost_usd=1.0)

    store.finish_agent_run(run_id, state="succeeded", ended_at=T0, cost_usd=9.0)

    (run,) = store.agent_runs()
    assert (run.state, run.cost_usd) == ("stopped", 1.0)


def test_a_run_cannot_be_finished_as_running_or_as_an_unknown_state(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    run_id = start(store)

    for state in ("running", "exploded"):
        with pytest.raises(ValueError):
            store.finish_agent_run(run_id, state=state, ended_at=T0)


def test_runs_still_running_when_toolbelt_stops_are_abandoned(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    live = start(store, task_id="a")
    done = start(store, task_id="b")
    store.finish_agent_run(done, state="succeeded", ended_at=T0)

    abandoned = store.abandon_running_agent_runs(T0 + timedelta(hours=1))

    by_id = {r.run_id: r for r in store.agent_runs()}
    assert abandoned == 1
    assert by_id[live].state == "abandoned"
    assert by_id[live].ended_at == T0 + timedelta(hours=1)
    assert by_id[done].state == "succeeded"


def test_runs_are_listed_newest_first_and_can_be_limited_by_start(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    old = start(store, at=T0)
    new = start(store, at=T0 + timedelta(hours=2))

    assert [r.run_id for r in store.agent_runs()] == [new, old]
    assert [r.run_id for r in store.agent_runs(since=T0 + timedelta(hours=1))] == [new]
