"""Unit tests for `update_repo` running its installs in the right directory.

`update_repo(target_path)` checks for `.tool-versions`/`uv.lock` under
`target_path`, so its `asdf install`/`uv sync` calls must actually run there
too (`cwd=target_path`) rather than inheriting whatever the process's ambient
cwd happens to be — real for a newly created worktree, whose directory
differs from wherever the command was invoked from. `subprocess.run` is faked
so this doesn't depend on asdf/uv actually being installed.
"""

import subprocess
from pathlib import Path

import pytest

from toolbelt.git import workflow


def _fake_run(calls: list[tuple[list[str], Path | None]]):
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("cwd")))
        return subprocess.CompletedProcess(cmd, 0)

    return run


def test_asdf_install_runs_in_target_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / ".tool-versions").write_text("python 3.12.0\n")
    calls: list[tuple[list[str], Path | None]] = []
    monkeypatch.setattr(workflow.subprocess, "run", _fake_run(calls))

    workflow.update_repo(tmp_path)

    assert calls == [(["asdf", "install"], tmp_path)]


def test_uv_sync_runs_in_target_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "uv.lock").write_text("")
    calls: list[tuple[list[str], Path | None]] = []
    monkeypatch.setattr(workflow.subprocess, "run", _fake_run(calls))

    workflow.update_repo(tmp_path)

    assert calls == [(["uv", "sync", "--all-groups"], tmp_path)]


def test_skips_both_commands_when_neither_file_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[list[str], Path | None]] = []
    monkeypatch.setattr(workflow.subprocess, "run", _fake_run(calls))

    workflow.update_repo(tmp_path)

    assert calls == []
