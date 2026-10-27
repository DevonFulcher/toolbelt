"""What agents do in a turn: the shell classifier, the chunk files the plugin
writes, and reading them into the store."""

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolbelt.agent_activity.chunks import UnreadableChunk, parse_chunk
from toolbelt.agent_activity.ingest import ActivityIngest
from toolbelt.agent_activity.shell import CATEGORIES, TOOL_CATEGORIES, classify
from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.task_claims.resolution import TrackedTask

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def iso(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize(
    ("command", "category", "program", "subcommand"),
    [
        ("cd /x && uv run pytest -q tests | tail -5", "test", "uv", "run"),
        ("git -C /a status", "vcs", "git", "status"),
        ("pnpm --dir web test --run", "test", "pnpm", "test"),
        ("pnpm build", "build", "pnpm", "build"),
        ("FOO=1 sudo rg foo src | head", "search", "rg", None),
        ("uv run ruff check src", "lint", "uv", "run"),
        ("gh pr view 12", "github", "gh", "pr"),
        ("curl -s localhost:8765/api | python3 -c 'print(1)'", "network", "curl", None),
        ("tt git save -m 'x && y'", "toolbelt", "tt", "git"),
        ("make lint", "lint", "make", "lint"),
        ("ls -la", "files", "ls", None),
        ("cargo test", "test", "cargo", "test"),
        ("npm install", "deps", "npm", "install"),
        ("claude plugin test claude-plugin", "test", "claude", "plugin"),
        ("timeout 120 uv run pytest -q tests", "test", "uv", "run"),
        ("timeout -k 5 90 .venv/bin/pytest -q a.py", "test", "pytest", None),
        ("timeout 900 gh run watch 1 --exit-status", "github", "gh", "run"),
        (
            "for i in $(seq 1 50); do s=$(gh pr checks 1 --json x); sleep 10; done",
            "github",
            "gh",
            "pr",
        ),
        (
            "end=$((SECONDS+540)); while [ $SECONDS -lt $end ]; do gh run view 1; done",
            "github",
            "gh",
            "run",
        ),
        ("cd /y", "other", None, None),
        ("echo hi", "other", None, None),
        ("", "other", None, None),
    ],
)
def test_a_bash_command_is_classified_by_its_main_program(
    command, category, program, subcommand
):
    result = classify("Bash", command)

    assert (result.category, result.program, result.subcommand) == (
        category,
        program,
        subcommand,
    )


def test_a_chain_lists_every_program_and_quotes_hide_operators():
    result = classify("Bash", "cd /x && make 'a;b' | tee out; git status")

    assert result.programs == ("cd", "make", "tee", "git")
    assert result.program == "make"


def test_other_tools_are_classified_by_kind():
    assert classify("Edit", "/x").category == "edit"
    assert classify("Grep", "p").category == "search"
    assert classify("Task", "").category == "subagent"
    assert classify("mcp__x__y", "").category == "mcp"
    assert classify("Whatever", "").category == "other"
    assert set(TOOL_CATEGORIES.values()) | {"vcs", "mcp", "other"} <= set(CATEGORIES)


def chunk_text(
    *,
    seq: int = 0,
    final: bool = True,
    turn: str = "t1",
    holder: str = "sess",
    calls: list[dict] | None = None,
    totals: dict | None = None,
) -> str:
    return json.dumps(
        {
            "version": 1,
            "holder": holder,
            "session_id": holder.split(".")[0],
            "agent_id": None,
            "turn_id": turn,
            "seq": seq,
            "final": final,
            "started_at": iso(0),
            "ended_at": iso(2) if final else None,
            "reason": "answer" if final else None,
            "totals": totals
            if totals is not None
            else (
                {
                    "wall_ms": 120_000,
                    "tool_ms": 30_000,
                    "delegated_ms": 0,
                    "blocked_ms": 10_000,
                    "model_ms": 80_000,
                }
                if final
                else None
            ),
            "calls": calls if calls is not None else [],
        }
    )


def call(
    tool: str = "Bash",
    detail: str = "git status",
    *,
    at: float = 0.5,
    ms: int = 1000,
    blocked: int = 0,
    worktree: str | None = None,
    **extra,
) -> dict:
    return {
        "tool": tool,
        "detail": detail,
        "started_at": iso(at),
        "duration_ms": ms,
        "blocked_ms": blocked,
        "ok": True,
        "unended": False,
        "worktree": worktree,
        "background": False,
        "delegated": False,
        **extra,
    }


def test_a_chunk_parses_with_its_totals_and_calls():
    chunk = parse_chunk(chunk_text(calls=[call()]))

    assert chunk.key == "sess/t1/0"
    assert chunk.final and chunk.totals is not None and chunk.totals.model_ms == 80_000
    assert [c.tool for c in chunk.calls] == ["Bash"]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"version": 2}),
        json.dumps({"version": 1, "holder": "h"}),
        chunk_text().replace('"totals"', '"nothing"'),
    ],
)
def test_a_chunk_that_cannot_be_used_is_unreadable(text):
    with pytest.raises(UnreadableChunk):
        parse_chunk(text)


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
    directory = tmp_path / "activity"
    ingest = ActivityIngest(
        directory=lambda: directory,
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
        wall_clock=lambda: NOW,
    )
    directory.mkdir()
    return ingest, directory, store, facts.task_id, repo_id, str(worktree)


def test_a_turn_is_stored_with_its_calls_attributed_and_classified(rig):
    ingest, directory, store, task_id, repo_id, worktree = rig
    (directory / "sess.t1.0.json").write_text(
        chunk_text(
            calls=[
                call(
                    "Bash", "uv run pytest -q", ms=4000, blocked=1000, worktree=worktree
                ),
                call("Read", "/x/a.py", at=1, ms=100, worktree=worktree),
                call("Bash", "ls", at=1.5, ms=50),
            ]
        )
    )

    assert ingest.poll() == 1

    (turn,) = store.agent_turns()
    assert (turn.task_id, turn.repo_id, turn.calls) == (task_id, repo_id, 3)
    assert turn.totals.model_ms == 80_000
    # Call time less the time blocked, by tool and by category.
    assert turn.by_tool == {"Bash": (2, 3050), "Read": (1, 100)}
    assert turn.by_category == {"test": (1, 3000), "read": (1, 100), "files": (1, 50)}
    assert list(directory.glob("*.json")) == []


def test_reading_a_chunk_twice_stores_it_once(rig):
    ingest, directory, store, *_ = rig
    text = chunk_text(calls=[call()])
    (directory / "a.json").write_text(text)
    ingest.poll()
    (directory / "a.json").write_text(text)

    assert ingest.poll() == 0
    assert store.agent_turns()[0].calls == 1


def test_a_long_turn_arrives_in_chunks_whatever_their_file_order(rig):
    ingest, directory, store, *_ = rig
    # seq 10 sorts before seq 2 by name: the turn's row must still hold all.
    (directory / "sess.t1.10.json").write_text(
        chunk_text(seq=10, calls=[call(detail="git log", at=3)])
    )
    (directory / "sess.t1.2.json").write_text(
        chunk_text(seq=2, final=False, calls=[call(detail="git diff", at=1)])
    )

    assert ingest.poll() == 2

    assert [t.calls for t in store.agent_turns()] == [2]


def test_a_chunk_that_arrives_after_its_turn_closed_is_added_to_it(rig):
    ingest, directory, store, *_ = rig
    (directory / "sess.t1.1.json").write_text(chunk_text(seq=1, calls=[call(at=2)]))
    ingest.poll()
    (directory / "sess.t1.0.json").write_text(
        chunk_text(seq=0, final=False, calls=[call(at=1), call(at=1.2)])
    )
    ingest.poll()

    assert [t.calls for t in store.agent_turns()] == [3]


def test_a_half_written_file_waits_and_an_old_unreadable_one_goes(rig):
    ingest, directory, *_ = rig
    fresh = directory / "fresh.json"
    old = directory / "old.json"
    fresh.write_text("{")
    old.write_text("{")
    long_ago = time.time() - 3600
    os.utime(old, (long_ago, long_ago))

    assert ingest.poll() == 0

    assert fresh.exists() and not old.exists()


def test_a_turn_whose_writer_died_is_closed_from_its_calls(rig):
    ingest, directory, store, *_ = rig
    (directory / "sess.t9.0.json").write_text(
        chunk_text(
            seq=0,
            final=False,
            turn="t9",
            calls=[call(at=-10, ms=60_000), call(at=-5, ms=30_000, blocked=10_000)],
        )
    )
    ingest.poll()
    assert store.agent_turns() == []

    assert (
        store.close_unfinished_turns(
            idle_for=timedelta(hours=1), now=NOW + timedelta(hours=2)
        )
        == 1
    )

    (turn,) = store.agent_turns(since=NOW - timedelta(days=1))
    assert turn.reason == "unfinished"
    assert turn.totals.tool_ms == 80_000 and turn.totals.blocked_ms == 10_000
    assert (
        turn.totals.tool_ms + turn.totals.blocked_ms + turn.totals.model_ms
        == turn.totals.wall_ms
    )


def test_raw_calls_expire_but_the_turn_stays(rig):
    ingest, directory, store, *_ = rig
    (directory / "a.json").write_text(chunk_text(calls=[call()]))
    ingest.poll()

    assert store.prune_tool_calls(NOW + timedelta(days=1)) == 1

    assert len(store.agent_turns()) == 1
    assert (
        store._conn.execute("SELECT COUNT(*) FROM agent_tool_calls").fetchone()[0] == 0
    )
