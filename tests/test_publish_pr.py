"""Publishing runs branch compression before marking a draft PR ready."""

import os
from pathlib import Path

from toolbelt.ui.publish_pr import publish_pr


def command_binaries(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    toolbelt = bin_dir / "toolbelt"
    toolbelt.write_text(
        '#!/bin/sh\nprintf "toolbelt %s\\n" "$*" >> "$PUBLISH_LOG"\n'
        'exit "${TOOLBELT_EXIT:-0}"\n'
    )
    toolbelt.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\nprintf "gh %s\\n" "$*" >> "$PUBLISH_LOG"\n')
    gh.chmod(0o755)
    return bin_dir


def test_draft_compresses_before_asking_github_to_mark_it_ready(
    repo: Path, tmp_path: Path, monkeypatch
):
    bin_dir = command_binaries(tmp_path)
    log = tmp_path / "commands.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PUBLISH_LOG", str(log))

    result = publish_pr(repo, "main", 17, is_draft=True)

    assert result.ok
    assert log.read_text().splitlines() == [
        "toolbelt git compress",
        "gh pr ready 17",
    ]


def test_a_ready_pr_only_compresses(repo: Path, tmp_path: Path, monkeypatch):
    bin_dir = command_binaries(tmp_path)
    log = tmp_path / "commands.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PUBLISH_LOG", str(log))

    result = publish_pr(repo, "main", 17, is_draft=False)

    assert result.ok
    assert log.read_text().splitlines() == ["toolbelt git compress"]


def test_compression_failure_does_not_mark_the_pr_ready(
    repo: Path, tmp_path: Path, monkeypatch
):
    bin_dir = command_binaries(tmp_path)
    log = tmp_path / "commands.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PUBLISH_LOG", str(log))
    monkeypatch.setenv("TOOLBELT_EXIT", "1")

    result = publish_pr(repo, "main", 17, is_draft=True)

    assert not result.ok
    assert log.read_text().splitlines() == ["toolbelt git compress"]
