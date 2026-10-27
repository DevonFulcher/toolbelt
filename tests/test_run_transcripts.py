"""An autonomous run's tool calls, read from its transcript into the store."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolbelt.agent_activity.ingest import ActivityIngest
from toolbelt.agent_activity.run_transcripts import RunTranscripts, run_chunk
from toolbelt.git.stack.store import (
    RepoIdentity,
    StackStore,
    TurnTotals,
    default_db_path,
)
from toolbelt.task_claims.resolution import TrackedTask

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def iso(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def use(call_id: str, tool: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": call_id, "name": tool, "input": tool_input}


def result(call_id: str, *, error: bool = False) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": "x",
        "is_error": error,
    }


def entry(minutes: float, cwd: str, *blocks: dict) -> dict:
    return {
        "timestamp": iso(minutes),
        "cwd": cwd,
        "message": {"role": "assistant", "content": list(blocks)},
    }


@pytest.fixture
def rig(tmp_path: Path):
    store = StackStore(default_db_path())
    repo_id = store.ensure_repo(
        RepoIdentity(
            git_common_dir=tmp_path / "r" / ".git",
            path=tmp_path / "r",
            remote_url=None,
        )
    )
    store.set_parent(repo_id, "feature", "main")
    (facts,) = store.tracked_branch_facts()
    worktree = tmp_path / "wt" / "r" / "feature"
    worktree.mkdir(parents=True)
    projects = tmp_path / "projects"
    (projects / "-wt-feature").mkdir(parents=True)
    ingest = ActivityIngest(
        directory=lambda: tmp_path / "activity",
        tasks=lambda: [
            TrackedTask(
                repo_id=repo_id,
                repo_name="r",
                branch="feature",
                task_id=facts.task_id,
                task_ref=None,
                worktree=worktree,
            )
        ],
        wall_clock=lambda: NOW + timedelta(hours=1),
    )
    feed = RunTranscripts(
        ingest=ingest,
        projects=lambda: projects,
        wall_clock=lambda: NOW + timedelta(hours=1),
    )

    def finished_run(
        session: str, started: float, ended: float, state: str = "succeeded"
    ):
        run_id = store.start_agent_run(
            task_id=facts.task_id,
            repo_id=repo_id,
            branch="feature",
            trigger="resolve_comments",
            head_sha="abc",
            model="m",
            at=NOW + timedelta(minutes=started),
            session_id=session,
        )
        store.finish_agent_run(
            run_id,
            state=state,
            ended_at=NOW + timedelta(minutes=ended),
            session_id=session,
        )
        run = store.agent_run(run_id)
        assert run is not None
        return run

    def transcript(session: str, entries: list[dict]) -> Path:
        path = projects / "-wt-feature" / f"{session}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return path

    return feed, store, str(worktree), finished_run, transcript, facts.task_id


def test_a_runs_calls_are_timed_from_the_transcript(rig):
    _, _, worktree, finished_run, transcript, _ = rig
    run = finished_run("s1", 0, 10)
    path = transcript(
        "s1",
        [
            # An earlier run's call in the same session is not this run's.
            entry(-30, worktree, use("old", "Bash", {"command": "ls"})),
            entry(-29, worktree, result("old")),
            # Two parallel calls: 1 to 4 and 1.5 to 3 minutes.
            entry(1, worktree, use("a", "Bash", {"command": "task test"})),
            entry(1.5, worktree, use("b", "Bash", {"command": "uv run ruff check"})),
            entry(3, worktree, result("b")),
            entry(4, worktree, result("a")),
            entry(5, worktree, use("c", "Edit", {"file_path": "/x/a.py"})),
            entry(5.5, worktree, result("c", error=True)),
            entry(6, worktree, use("d", "Task", {"description": "look"})),
            entry(8, worktree, result("d")),
            # Still running when the run ended.
            entry(
                9,
                worktree,
                use(
                    "e",
                    "Bash",
                    {"command": "gh run watch 1", "run_in_background": True},
                ),
            ),
        ],
    )

    chunk = run_chunk(run, path)

    assert [
        (c.tool, c.detail, c.duration_ms, c.ok, c.unended, c.background, c.delegated)
        for c in chunk.calls
    ] == [
        ("Bash", "task test", 180_000, True, False, False, False),
        ("Bash", "uv run ruff check", 90_000, True, False, False, False),
        ("Edit", "/x/a.py", 30_000, False, False, False, False),
        ("Task", "look", 120_000, True, False, False, True),
        ("Bash", "gh run watch 1", 60_000, None, True, True, False),
    ]
    assert {c.worktree for c in chunk.calls} == {worktree}
    # The parallel calls count once: 3 + 0.5 + 1 minutes of tools, 2 of
    # subagent, and the rest of the 10 minutes is the model's.
    totals = chunk.totals
    assert totals is not None
    assert (totals.wall_ms, totals.tool_ms, totals.delegated_ms, totals.model_ms) == (
        600_000,
        270_000,
        120_000,
        210_000,
    )
    assert (chunk.holder, chunk.turn_id, chunk.final, chunk.reason) == (
        "s1",
        f"run-{run.run_id}",
        True,
        "succeeded",
    )


def test_a_finished_run_is_stored_once_and_tied_to_its_task(rig):
    feed, store, worktree, finished_run, transcript, task_id = rig
    run = finished_run("s1", 0, 10)
    transcript(
        "s1",
        [
            entry(1, worktree, use("a", "Bash", {"command": "task test"})),
            entry(4, worktree, result("a")),
        ],
    )

    assert feed.poll() == 1
    assert feed.poll() == 0

    (turn,) = store.agent_turns()
    assert (turn.holder, turn.turn_id, turn.task_id, turn.calls) == (
        "s1",
        f"run-{run.run_id}",
        task_id,
        1,
    )
    assert turn.by_category["test"] == (1, 180_000)


def test_runs_that_resume_one_session_are_stored_each_from_its_own_window(rig):
    feed, store, worktree, finished_run, transcript, _ = rig
    first = finished_run("s1", 0, 10, state="deciding")
    second = finished_run("s1", 60, 70)
    transcript(
        "s1",
        [
            entry(1, worktree, use("a", "Bash", {"command": "ls"})),
            entry(2, worktree, result("a")),
            entry(61, worktree, use("b", "Bash", {"command": "pwd"})),
            entry(62, worktree, result("b")),
            entry(63, worktree, use("c", "Bash", {"command": "pwd"})),
            entry(64, worktree, result("c")),
        ],
    )

    assert feed.poll() == 2

    calls = {turn.turn_id: turn.calls for turn in store.agent_turns()}
    assert calls == {f"run-{first.run_id}": 1, f"run-{second.run_id}": 2}


def test_a_run_the_plugin_recorded_is_not_stored_again(rig):
    feed, store, worktree, finished_run, transcript, _ = rig
    run = finished_run("s1", 0, 10)
    transcript("s1", [entry(1, worktree, use("a", "Bash", {"command": "ls"}))])
    store.ingest_activity_chunk(
        "s1/t1/0",
        [],
        turn=(
            "s1",
            "t1",
            "s1",
            None,
            NOW,
            NOW + timedelta(minutes=1),
            "answer",
            TurnTotals(
                wall_ms=60_000, tool_ms=0, delegated_ms=0, blocked_ms=0, model_ms=60_000
            ),
        ),
        at=NOW,
    )

    assert feed.poll() == 0
    assert [turn.turn_id for turn in store.agent_turns()] == ["t1"]
    assert run.run_id not in {turn.turn_id for turn in store.agent_turns()}


def test_a_run_without_a_transcript_or_still_running_is_skipped(rig):
    feed, store, _, finished_run, _, task_id = rig
    finished_run("gone", 0, 10)
    store.start_agent_run(
        task_id=task_id,
        repo_id=1,
        branch="feature",
        trigger="fix_ci",
        head_sha="abc",
        model="m",
        at=NOW,
        session_id="going",
    )

    assert feed.poll() == 0
    assert store.agent_turns() == []
