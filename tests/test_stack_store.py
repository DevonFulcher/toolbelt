"""Tests for the SQLite stack store, the legacy git-config import, and
multi-repo `tree`."""

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import git
from test_cli import _invoke
from toolbelt.git.stack import lineage, store
from toolbelt.git.stack.store import RepoIdentity, StackStore


def _identity(tmp_path: Path, name: str = "repo") -> RepoIdentity:
    path = tmp_path / name
    return RepoIdentity(git_common_dir=path / ".git", path=path, remote_url=None)


def test_fresh_db_is_migrated_to_latest_version(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")
    assert s.schema_version() == len(store._MIGRATIONS)
    # Reopening is idempotent.
    s.close()
    assert StackStore(tmp_path / "stacks.db").schema_version() == len(store._MIGRATIONS)


def test_migrations_apply_on_top_of_an_older_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "stacks.db"
    StackStore(db).close()
    # Simulate a later release that appends a migration.
    extra = ("ALTER TABLE branches ADD COLUMN jira_key TEXT",)
    monkeypatch.setattr(store, "_MIGRATIONS", [*store._MIGRATIONS, extra])
    s = StackStore(db)
    assert s.schema_version() == 2
    columns = [
        row[1] for row in s._conn.execute("PRAGMA table_info(branches)").fetchall()
    ]
    assert "jira_key" in columns


def test_wal_mode_enabled(tmp_path: Path):
    db = tmp_path / "stacks.db"
    StackStore(db).close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_parents_are_scoped_per_repo(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")
    a = s.ensure_repo(_identity(tmp_path, "a"), dict)
    b = s.ensure_repo(_identity(tmp_path, "b"), dict)
    s.set_parent(a, "feat", "main")
    s.set_parent(b, "feat", "develop")
    assert s.get_parent(a, "feat") == "main"
    assert s.get_parent(b, "feat") == "develop"
    s.set_parent(a, "feat", "other")
    assert s.all_parents(a) == {"feat": "other"}
    s.remove_branch(a, "feat")
    assert s.get_parent(a, "feat") is None
    assert s.get_parent(b, "feat") == "develop"


def test_import_runs_once_per_repo(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")
    identity = _identity(tmp_path)
    repo_id = s.ensure_repo(identity, lambda: {"feat": "main"})
    assert s.all_parents(repo_id) == {"feat": "main"}

    s.remove_branch(repo_id, "feat")

    def must_not_load() -> dict[str, str]:
        raise AssertionError("legacy data imported twice")

    assert s.ensure_repo(identity, must_not_load) == repo_id
    assert s.all_parents(repo_id) == {}


def test_existing_git_config_is_imported_and_left_untouched(repo: Path):
    git("config", "toolbelt-stack.devon/api.parent", "main", cwd=repo)
    git("config", "toolbelt-stack.devon/api_tests.parent", "devon/api", cwd=repo)

    assert lineage.all_parents(root=repo) == {
        "devon/api": "main",
        "devon/api_tests": "devon/api",
    }

    # git config was neither modified nor deleted...
    assert git("config", "--get", "toolbelt-stack.devon/api.parent", cwd=repo) == "main"
    # ...and later writes go to the DB only.
    lineage.set_parent("devon/new", "main", root=repo)
    lineage.remove_parent("devon/api", root=repo)
    assert git("config", "--get", "toolbelt-stack.devon/api.parent", cwd=repo) == "main"
    assert "toolbelt-stack.devon/new" not in git("config", "--list", cwd=repo)
    assert lineage.get_parent("devon/api", root=repo) is None
    assert lineage.get_parent("devon/new", root=repo) == "main"


def test_worktrees_share_one_repo_record(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt"
    git("worktree", "add", "-b", "devon/x", str(wt), cwd=repo)
    lineage.set_parent("devon/x", "main", root=repo)
    assert lineage.get_parent("devon/x", root=wt) == "main"
    assert len(lineage.known_repos()) == 1


def test_tree_shows_multiple_repos(
    repo: Path, tmp_path: Path, capfd: pytest.CaptureFixture
):
    other = tmp_path / "other-repo"
    other.mkdir()
    git("init", "-b", "main", cwd=other)
    lineage.set_parent("devon/api", "main", root=repo)
    lineage.set_parent("devon/docs", "main", root=other)

    result = _invoke(["tree"], cwd=repo, capfd=capfd)

    assert f"other-repo ({other.resolve()})" in result.output
    assert f"repo ({repo.resolve()})" in result.output
    assert "devon/api" in result.output
    assert "devon/docs" in result.output


def test_tree_outside_a_repo_shows_known_repos(
    repo: Path, tmp_path: Path, capfd: pytest.CaptureFixture
):
    lineage.set_parent("devon/api", "main", root=repo)
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _invoke(["tree"], cwd=outside, capfd=capfd)

    assert f"repo ({repo.resolve()})" in result.output
    assert "devon/api" in result.output


def test_tree_marks_cwd_branch_and_skips_repos_whose_path_is_gone(
    repo: Path, tmp_path: Path, capfd: pytest.CaptureFixture
):
    gone = tmp_path / "gone-repo"
    gone.mkdir()
    git("init", "-b", "main", cwd=gone)
    lineage.set_parent("devon/docs", "main", root=gone)
    lineage.set_parent("main", "origin-base", root=repo)
    shutil.rmtree(gone)

    result = _invoke(["tree"], cwd=repo, capfd=capfd)

    assert "main *" in result.output
    assert "devon/docs" not in result.output
    assert f"gone-repo ({gone.resolve()})" not in result.output
