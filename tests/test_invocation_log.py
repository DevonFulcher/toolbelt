"""Tests for the persistent invocation log.

`install()` sets the module-level active log, so tests that use it restore it
afterwards.
"""

import json
import logging
import asyncio
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from toolbelt import invocation_log, logged_process
from toolbelt.cli import app
from toolbelt.invocation_log import InvocationLog, JsonlWriter


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _make_log(tmp_path: Path, **env: str) -> InvocationLog:
    return InvocationLog.from_env({"TOOLBELT_LOG_DIR": str(tmp_path), **env})


@pytest.fixture
def restore_process_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(invocation_log, "active", None)
    monkeypatch.delenv(invocation_log.INVOCATION_ID_ENV, raising=False)
    handlers = list(logging.getLogger("toolbelt").handlers)
    yield
    logging.getLogger("toolbelt").handlers[:] = handlers


def test_log_dir_precedence() -> None:
    assert invocation_log.log_dir({"TOOLBELT_LOG_DIR": "/x"}) == Path("/x")
    assert invocation_log.log_dir({"XDG_STATE_HOME": "/s"}) == Path("/s/toolbelt")
    assert invocation_log.log_dir({}) == Path.home() / ".local/state/toolbelt"


def test_parent_invocation_comes_from_env(tmp_path: Path) -> None:
    log = _make_log(tmp_path, TOOLBELT_INVOCATION_ID="parent-1")
    log.event("x")
    rec = _records(log.writer.path)[0]
    assert rec["parent_inv"] == "parent-1"
    assert rec["inv"] == log.invocation_id != "parent-1"


def test_rotation_bounds_files_and_keeps_newest(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path / "t.jsonl", max_bytes=200, backups=2)
    for i in range(100):
        writer.write({"i": i, "pad": "x" * 20})
    names = sorted(p.name for p in tmp_path.iterdir() if not p.name.endswith(".lock"))
    assert names == ["t.jsonl", "t.jsonl.1", "t.jsonl.2"]
    assert json.loads((tmp_path / "t.jsonl").read_text().splitlines()[-1])["i"] == 99


def test_concurrent_writers_produce_whole_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"

    def worker(n: int) -> None:
        w = JsonlWriter(path, max_bytes=10**9, backups=1)
        for i in range(50):
            w.write({"w": n, "i": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_records(path)) == 400


def test_unwritable_dir_is_swallowed(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    writer = JsonlWriter(blocker / "sub" / "t.jsonl", max_bytes=100, backups=1)
    writer.write({"a": 1})
    assert writer.disabled
    writer.write({"a": 2})  # no raise


def test_subprocess_exec_and_exit_are_logged(
    tmp_path: Path, restore_process_state: None
) -> None:
    log = _make_log(tmp_path)
    invocation_log.install(log)
    logged_process.run([sys.executable, "-c", "raise SystemExit(3)"])
    logging.getLogger("toolbelt").info("hello")
    by_event = {r["event"]: r for r in _records(log.writer.path)}
    assert by_event["exec"]["cmd"][0] == sys.executable
    assert by_event["exec_exit"]["returncode"] == 3
    assert by_event["exec_exit"]["child_pid"] == by_event["exec"]["child_pid"]
    assert by_event["log"]["message"] == "hello"


def test_run_behaves_like_subprocess_run(
    tmp_path: Path, restore_process_state: None
) -> None:
    invocation_log.install(_make_log(tmp_path))
    result = logged_process.run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        input="hi",
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "HI"
    with pytest.raises(subprocess.CalledProcessError):
        logged_process.run([sys.executable, "-c", "raise SystemExit(1)"], check=True)
    assert (
        logged_process.check_output(
            [sys.executable, "-c", "print('x')"], text=True
        ).strip()
        == "x"
    )


def test_failed_exec_is_logged(tmp_path: Path, restore_process_state: None) -> None:
    log = _make_log(tmp_path)
    invocation_log.install(log)
    with pytest.raises(FileNotFoundError):
        logged_process.run(["definitely-not-a-real-command-xyz"])
    assert [r["event"] for r in _records(log.writer.path)] == ["exec_failed"]


def test_asyncio_exec_and_exit_are_logged(
    tmp_path: Path, restore_process_state: None
) -> None:
    log = _make_log(tmp_path)
    invocation_log.install(log)

    async def go() -> bytes:
        process = await logged_process.create_subprocess_exec(
            sys.executable,
            "-c",
            "print('out'); raise SystemExit(4)",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        assert process.returncode == 4
        return stdout

    assert asyncio.run(go()).strip() == b"out"
    by_event = {r["event"]: r for r in _records(log.writer.path)}
    assert by_event["exec_exit"]["returncode"] == 4
    assert by_event["exec_exit"]["child_pid"] == by_event["exec"]["child_pid"]


def test_unlogged_when_no_active_log() -> None:
    assert invocation_log.active is None
    result = logged_process.run(
        [sys.executable, "-c", "print(1)"], capture_output=True, text=True
    )
    assert result.stdout.strip() == "1"


def test_run_logged_records_exit_and_crash(tmp_path: Path) -> None:
    log = _make_log(tmp_path)

    def exits() -> None:
        raise typer.Exit(2)

    def crashes() -> None:
        raise RuntimeError("boom")

    with pytest.raises(typer.Exit):
        invocation_log.run_logged(exits, log, ["tt", "x"])
    with pytest.raises(RuntimeError):
        invocation_log.run_logged(crashes, log, ["tt", "y"])
    records = _records(log.writer.path)
    assert records[0]["argv"] == ["tt", "x"]
    assert [r["event"] for r in records].count("start") == 2
    assert "RuntimeError: boom" in records[-1]["traceback"]


def test_logs_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLBELT_LOG_DIR", str(tmp_path))
    log = InvocationLog.from_env()
    log.event("a")
    InvocationLog.from_env().event("b")
    runner = CliRunner()
    result = runner.invoke(app, ["logs", "--path"])
    assert result.stdout.strip() == str(tmp_path / invocation_log.LOG_FILENAME)
    result = runner.invoke(app, ["logs", "-i", log.invocation_id])
    assert [json.loads(x)["event"] for x in result.stdout.splitlines()] == ["a"]
