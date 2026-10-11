"""Tests for the SQLite stack store, the legacy git-config import, and
multi-repo `tree`."""

import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import git
from test_cli import _invoke
from toolbelt.git.stack import lineage, store
from toolbelt.git.stack.store import BranchLinks, RepoIdentity, StackStore


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
    assert s.schema_version() == len(store._MIGRATIONS)
    columns = [
        row[1] for row in s._conn.execute("PRAGMA table_info(branches)").fetchall()
    ]
    assert "jira_key" in columns


def test_links_are_nullable_kept_on_reparent_and_scoped_to_the_branch(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")
    repo_id = s.ensure_repo(_identity(tmp_path))
    s.set_parent(repo_id, "feat", "main")
    assert s.get_links(repo_id, "feat") == BranchLinks(None, None, None)
    assert s.get_links(repo_id, "nope") is None

    assert s.set_links(repo_id, "feat", task="ABC-1", task_tracker="jira")
    assert s.set_links(repo_id, "feat", agent_link="claude://s/1")
    s.set_parent(repo_id, "feat", "develop")  # reparent keeps links
    assert s.get_links(repo_id, "feat") == BranchLinks("ABC-1", "jira", "claude://s/1")
    assert not s.set_links(repo_id, "nope", task="X-1")

    # A new task replaces the old one *and* the tracker it was stored under.
    s.set_parent(repo_id, "feat", "develop", task="URL-ONLY")
    assert s.get_links(repo_id, "feat") == BranchLinks("URL-ONLY", None, "claude://s/1")


def test_links_migration_applies_to_a_v1_database(tmp_path: Path):
    db = tmp_path / "stacks.db"
    with sqlite3.connect(db) as conn:
        for statement in store._MIGRATIONS[0]:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO repos (git_common_dir, path, name, created_at) "
            "VALUES ('/r/.git', '/r', 'r', 'now')"
        )
        conn.execute(
            "INSERT INTO branches (repo_id, name, parent, created_at, updated_at) "
            "VALUES (1, 'old', 'main', 'now', 'now')"
        )
    s = StackStore(db)
    assert s.schema_version() == len(store._MIGRATIONS)
    assert s.get_links(1, "old") == BranchLinks(None, None, None)


def test_task_migration_renames_jira_keeping_data_and_tags_the_tracker(
    tmp_path: Path,
):
    """A database at the schema that still had a ``jira`` column."""
    db = tmp_path / "stacks.db"
    jira_era = store._MIGRATIONS[:3]
    with sqlite3.connect(db) as conn:
        for migration in jira_era:
            for statement in migration:
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {len(jira_era)}")
        conn.execute(
            "INSERT INTO repos (git_common_dir, path, name, created_at) "
            "VALUES ('/r/.git', '/r', 'r', 'now')"
        )
        conn.executemany(
            "INSERT INTO branches "
            "(repo_id, name, parent, jira, agent_link, created_at, updated_at) "
            "VALUES (1, ?, 'main', ?, ?, 'now', 'now')",
            [
                ("with-ticket", "ABC-1", "claude://s/1"),
                ("with-url", "https://j.example/browse/XYZ-9", None),
                ("without", None, None),
            ],
        )

    s = StackStore(db)

    assert s.schema_version() == len(store._MIGRATIONS)
    assert s.get_links(1, "with-ticket") == BranchLinks("ABC-1", "jira", "claude://s/1")
    assert s.get_links(1, "with-url") == BranchLinks(
        "https://j.example/browse/XYZ-9", "jira", None
    )
    assert s.get_links(1, "without") == BranchLinks(None, None, None)
    columns = [r[1] for r in s._conn.execute("PRAGMA table_info(branches)")]
    assert "jira" not in columns and {"task", "task_tracker"} <= set(columns)


def test_wal_mode_enabled(tmp_path: Path):
    db = tmp_path / "stacks.db"
    StackStore(db).close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_parents_are_scoped_per_repo(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")
    a = s.ensure_repo(_identity(tmp_path, "a"))
    b = s.ensure_repo(_identity(tmp_path, "b"))
    s.set_parent(a, "feat", "main")
    s.set_parent(b, "feat", "develop")
    assert s.get_parent(a, "feat") == "main"
    assert s.get_parent(b, "feat") == "develop"
    s.set_parent(a, "feat", "other")
    assert s.all_parents(a) == {"feat": "other"}
    s.remove_branch(a, "feat")
    assert s.get_parent(a, "feat") is None
    assert s.get_parent(b, "feat") == "develop"


def test_import_parents_never_overwrites_existing_rows(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")
    repo_id = s.ensure_repo(_identity(tmp_path))
    assert s.ensure_repo(_identity(tmp_path)) == repo_id
    s.set_parent(repo_id, "feat", "main")
    s.import_parents(repo_id, {"feat": "other", "new": "main"})
    assert s.all_parents(repo_id) == {"feat": "main", "new": "main"}


def _legacy_keys(repo: Path) -> str:
    return subprocess.run(
        ["git", "config", "--get-regexp", "^toolbelt-stack"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_first_import_copies_legacy_config_then_unsets_it(repo: Path):
    git("config", "toolbelt-stack.devon/api.parent", "main", cwd=repo)
    git("config", "toolbelt-stack.devon/api_tests.parent", "devon/api", cwd=repo)

    assert lineage.all_parents(root=repo) == {
        "devon/api": "main",
        "devon/api_tests": "devon/api",
    }

    assert _legacy_keys(repo) == ""
    # Later writes go to the DB only.
    lineage.set_parent("devon/new", "main", root=repo)
    assert "toolbelt-stack" not in git("config", "--list", cwd=repo)
    assert lineage.get_parent("devon/new", root=repo) == "main"


def test_legacy_keys_reappearing_are_imported_on_next_process(repo: Path):
    lineage.set_parent("devon/api", "main", root=repo)
    # An older toolbelt writes git config after the DB was already in use.
    git("config", "toolbelt-stack.devon/late.parent", "devon/api", cwd=repo)

    # Same process: already checked, so this is not re-read.
    assert lineage.get_parent("devon/late", root=repo) is None
    assert _legacy_keys(repo) != ""

    lineage.recheck_legacy_config()  # simulates the next `tt` process
    assert lineage.get_parent("devon/late", root=repo) == "devon/api"
    assert _legacy_keys(repo) == ""


def test_db_wins_over_conflicting_legacy_config(repo: Path):
    lineage.set_parent("devon/api", "main", root=repo)
    git("config", "toolbelt-stack.devon/api.parent", "stale-parent", cwd=repo)
    lineage.recheck_legacy_config()

    assert lineage.get_parent("devon/api", root=repo) == "main"
    assert _legacy_keys(repo) == ""


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


def test_started_at_is_now_on_first_insert_and_reparenting_keeps_it(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")
    repo_id = s.ensure_repo(_identity(tmp_path))
    first = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    later = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

    assert s.get_started_at(repo_id, "feat") is None  # untracked
    before = datetime.now(timezone.utc)
    s.set_parent(repo_id, "now", "main")
    s.import_parents(repo_id, {"imported": "main"})
    after = datetime.now(timezone.utc)
    s.set_parent(repo_id, "feat", "main", started_at=first)
    s.set_parent(repo_id, "feat", "develop", started_at=later)  # reparent

    assert s.get_started_at(repo_id, "feat") == first
    assert s.get_parent(repo_id, "feat") == "develop"
    for name in ("now", "imported"):
        started = s.get_started_at(repo_id, name)
        assert started is not None and before <= started <= after
    registered = s.get_started_at(repo_id, "now")
    s.set_parent(repo_id, "now", "develop")
    assert s.get_started_at(repo_id, "now") == registered


def test_started_at_migration_backfills_existing_rows_from_created_at(tmp_path: Path):
    db = tmp_path / "stacks.db"
    before = store._MIGRATIONS[:4]  # everything before the started_at column
    with sqlite3.connect(db) as conn:
        for migration in before:
            for statement in migration:
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {len(before)}")
        conn.execute(
            "INSERT INTO repos (git_common_dir, path, name, created_at) "
            "VALUES ('/r/.git', '/r', 'r', '2026-10-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO branches (repo_id, name, parent, created_at, updated_at) "
            "VALUES (1, 'old', 'main', '2026-10-02T00:00:00+00:00', 'now')"
        )

    s = StackStore(db)

    assert s.schema_version() == len(store._MIGRATIONS)
    assert s.get_started_at(1, "old") == datetime(2026, 10, 2, tzinfo=timezone.utc)
    s.set_parent(1, "old", "develop")  # reparenting keeps it
    assert s.get_started_at(1, "old") == datetime(2026, 10, 2, tzinfo=timezone.utc)


def test_removing_a_branch_archives_its_task_facts(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")
    repo_id = s.ensure_repo(_identity(tmp_path))
    started = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    s.set_parent(
        repo_id, "feat", "main", task="PLAT-1", task_tracker="jira", started_at=started
    )
    s.set_parent(repo_id, "other", "main")

    before = datetime.now(timezone.utc)
    s.remove_branch(repo_id, "feat", landed=True)
    s.remove_branch(repo_id, "other")
    s.remove_branch(repo_id, "never-tracked")

    assert s.get_parent(repo_id, "feat") is None
    feat, other = s.removed_branches()
    assert (feat.name, feat.task, feat.task_tracker) == ("feat", "PLAT-1", "jira")
    assert feat.started_at == started
    assert feat.landed is True and before <= feat.removed_at
    assert (other.name, other.task, other.landed) == ("other", None, False)
    assert other.started_at is not None  # stamped when it was registered


def test_the_archive_outlives_the_branch_and_is_scoped_by_repo_id(tmp_path: Path):
    db = tmp_path / "stacks.db"
    s = StackStore(db)
    a = s.ensure_repo(_identity(tmp_path, "a"))
    b = s.ensure_repo(_identity(tmp_path, "b"))
    s.set_parent(a, "feat", "main")
    s.set_parent(b, "feat", "main")
    s.remove_branch(a, "feat")
    s.close()

    reopened = StackStore(db)

    assert [(r.repo_id, r.name) for r in reopened.removed_branches()] == [(a, "feat")]
    assert [(t.repo_id, t.name) for t in reopened.tracked_branch_facts()] == [
        (b, "feat")
    ]


def test_archiving_began_when_the_table_was_created(tmp_path: Path):
    before = datetime.now(timezone.utc)
    s = StackStore(tmp_path / "stacks.db")
    after = datetime.now(timezone.utc)

    assert before.replace(microsecond=0) <= s.archive_since() <= after
    # Reopening does not reset it.
    s.close()
    assert StackStore(tmp_path / "stacks.db").archive_since() <= after
