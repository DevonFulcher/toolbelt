"""Tests for the background fast-forward pulls of `sync.repos`."""

import json
import subprocess
from pathlib import Path

from conftest import git

from toolbelt.git.repo_pull import (
    default_config_path,
    load_sync_repos,
    pull_repo,
    run_pulls,
    take_unreported_summary,
)


def _push_from_second_clone(git_remote: Path, tmp_path: Path, filename: str) -> None:
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(git_remote), str(other)], check=True, capture_output=True
    )
    git("config", "user.email", "o@example.com", cwd=other)
    git("config", "user.name", "Other", cwd=other)
    (other / filename).write_text("x\n")
    git("add", "-A", cwd=other)
    git("commit", "-m", f"add {filename}", cwd=other)
    git("push", "origin", "main", cwd=other)


def test_load_sync_repos_resolves_relative_paths_against_workdir(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(f"sync:\n  repos:\n    - proj\n    - {tmp_path}/abs\n")

    assert load_sync_repos(config, workdir=tmp_path / "work") == [
        tmp_path / "work" / "proj",
        tmp_path / "abs",
    ]


def test_default_config_path_honors_xdg_config_home():
    assert default_config_path({"XDG_CONFIG_HOME": "/c"}) == Path(
        "/c/toolbelt/config.yaml"
    )
    assert default_config_path({}) == Path.home() / ".config/toolbelt/config.yaml"
    assert default_config_path({"XDG_CONFIG_HOME": ""}) == (
        Path.home() / ".config/toolbelt/config.yaml"
    )


def test_load_sync_repos_without_config_is_empty(tmp_path: Path):
    assert load_sync_repos(tmp_path / "missing.yaml") == []


def test_fast_forwards_clean_default_branch(
    repo: Path, git_remote: Path, tmp_path: Path
):
    _push_from_second_clone(git_remote, tmp_path, "new.txt")

    result = pull_repo(repo)

    assert result.outcome == "pulled"
    assert (repo / "new.txt").exists()


def test_skips_dirty_checkout(repo: Path, git_remote: Path, tmp_path: Path):
    _push_from_second_clone(git_remote, tmp_path, "new.txt")
    (repo / "README.md").write_text("local edit\n")

    result = pull_repo(repo)

    assert result.outcome == "skipped"
    assert not (repo / "new.txt").exists()


def test_skips_non_default_branch(repo: Path, git_remote: Path, tmp_path: Path):
    _push_from_second_clone(git_remote, tmp_path, "new.txt")
    git("checkout", "-b", "feature", cwd=repo)

    result = pull_repo(repo)

    assert result.outcome == "skipped"
    assert "feature" in result.detail


def test_skips_diverged_branch(repo: Path, git_remote: Path, tmp_path: Path):
    _push_from_second_clone(git_remote, tmp_path, "new.txt")
    (repo / "local.txt").write_text("local\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "local commit", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)

    result = pull_repo(repo)

    assert result.outcome == "skipped"
    assert git("rev-parse", "HEAD", cwd=repo) == head


def test_run_pulls_records_status_reported_once(repo: Path, tmp_path: Path):
    state_dir = tmp_path / "state"

    run_pulls([repo], state_dir=state_dir)

    status = json.loads((state_dir / "sync-pull-status.json").read_text())
    assert status["results"][0]["outcome"] == "up-to-date"
    assert take_unreported_summary(state_dir=state_dir) == (
        "Background repo pulls: repo: up-to-date"
    )
    assert take_unreported_summary(state_dir=state_dir) is None
