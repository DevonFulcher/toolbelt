"""The stack DB's migration list: applied from scratch, and from every
historical schema version with rows in place (what a user's existing
``stacks.db`` looks like when they upgrade toolbelt)."""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from toolbelt.git.stack import store
from toolbelt.git.stack.store import BranchLinks, StackStore

CREATED = "2026-01-01T00:00:00+00:00"
STARTED = "2026-01-02T03:04:05+00:00"
REMOVED = "2026-02-01T00:00:00+00:00"

VERSIONS = range(1, len(store._MIGRATIONS) + 1)


def _build_at(db: Path, version: int) -> sqlite3.Connection:
    """A database exactly as it was at schema ``version``."""
    conn = sqlite3.connect(db, isolation_level=None)
    for migration in store._MIGRATIONS[:version]:
        for statement in migration:
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {version}")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    """Insert ``values`` keeping only the columns this schema version has. The
    task column was once called ``jira``."""
    columns = _columns(conn, table)
    if "jira" in columns and "task" in values:
        values = {("jira" if k == "task" else k): v for k, v in values.items()}
    kept = {k: v for k, v in values.items() if k in columns}
    conn.execute(
        f"INSERT INTO {table} ({', '.join(kept)}) "
        f"VALUES ({', '.join('?' for _ in kept)})",
        list(kept.values()),
    )


def _seed(conn: sqlite3.Connection) -> None:
    """One repo with a tracked branch carrying every link, a plain one, an
    archived one and a published chain, as far as this version can hold."""
    _insert(
        conn,
        "repos",
        {
            "id": 1,
            "git_common_dir": "/r/.git",
            "path": "/r",
            "name": "r",
            "remote_url": "git@github.com:me/r.git",
            "created_at": CREATED,
            "gh_stack_unsupported_at": REMOVED,
        },
    )
    for name, parent, task, link, started in (
        ("feat", "main", "ABC-1", "claude://s/1", STARTED),
        ("plain", "feat", None, None, None),
    ):
        _insert(
            conn,
            "branches",
            {
                "repo_id": 1,
                "name": name,
                "parent": parent,
                "task": task,
                "task_tracker": "jira" if task else None,
                "agent_link": link,
                "started_at": started,
                "created_at": CREATED,
                "updated_at": CREATED,
            },
        )
    if "removed_branches" in _tables(conn):
        _insert(
            conn,
            "removed_branches",
            {
                "repo_id": 1,
                "name": "old",
                "task": "OLD-7",
                "task_tracker": "jira",
                "started_at": STARTED,
                "removed_at": REMOVED,
                "landed": 1,
            },
        )
    if "published_chains" in _tables(conn):
        _insert(
            conn,
            "published_chains",
            {
                "repo_id": 1,
                "bottom": "feat",
                "chain_hash": "abc",
                "branches": "feat\nplain",
                "published_at": CREATED,
            },
        )


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_schema_version_is_the_number_of_migrations(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")

    assert s.schema_version() == len(store._MIGRATIONS)


def test_a_fresh_database_has_every_table_and_no_rows(tmp_path: Path):
    s = StackStore(tmp_path / "stacks.db")

    assert {
        "repos",
        "branches",
        "published_chains",
        "removed_branches",
        "archive_info",
    } <= _tables(s._conn)
    assert s.repos() == []
    assert s.removed_branches() == []
    assert s.tracked_branch_facts() == []


def test_migrating_twice_changes_nothing(tmp_path: Path):
    db = tmp_path / "stacks.db"
    conn = _build_at(db, 1)
    _seed(conn)
    conn.close()
    StackStore(db).close()

    s = StackStore(db)

    assert s.schema_version() == len(store._MIGRATIONS)
    assert s.get_parent(1, "feat") == "main"


@pytest.mark.parametrize("version", VERSIONS)
def test_every_historical_version_migrates_to_head_keeping_its_data(
    tmp_path: Path, version: int
):
    db = tmp_path / "stacks.db"
    conn = _build_at(db, version)
    _seed(conn)
    conn.close()

    s = StackStore(db)

    assert s.schema_version() == len(store._MIGRATIONS)
    [repo] = s.repos()
    assert (repo.id, repo.name, repo.path) == (1, "r", Path("/r"))
    assert s.all_parents(1) == {"feat": "main", "plain": "feat"}
    # Links: a Jira ref and the agent link arrived with version 2, the
    # tracker's name with version 4 (every older ref was a Jira one).
    if version >= 2:
        assert s.get_links(1, "feat") == BranchLinks("ABC-1", "jira", "claude://s/1")
    else:
        assert s.get_links(1, "feat") == BranchLinks(None, None, None)
    assert s.get_links(1, "plain") == BranchLinks(None, None, None)
    # The start time: backfilled from the creation time before version 5.
    expected_start = STARTED if version >= 5 else CREATED
    assert s.get_started_at(1, "feat") == datetime.fromisoformat(expected_start)
    assert s.get_started_at(1, "plain") == (
        datetime.fromisoformat(CREATED) if version < 5 else None
    )
    if version >= 3:
        assert s.gh_stack_unsupported_at(1) == datetime.fromisoformat(REMOVED)
        chain = s.get_published_chain(1, "feat")
        assert chain is not None and chain.branches == ["feat", "plain"]
    if version >= 6:
        [old] = s.removed_branches()
        assert (old.name, old.task, old.landed) == ("old", "OLD-7", True)
        assert old.removed_at == datetime.fromisoformat(REMOVED)
    else:
        assert s.removed_branches() == []
    # The migrated database is fully usable: archive a branch, add one.
    s.remove_branch(1, "plain")
    s.set_parent(1, "new", "main")
    assert s.all_parents(1) == {"feat": "main", "new": "main"}
    assert {b.name for b in s.removed_branches()} >= {"plain"}


def test_each_migration_applies_to_a_database_one_version_behind(tmp_path: Path):
    """Stepwise, not just all at once: version ``k`` -> ``k + 1`` works with
    rows present, for every ``k`` (``user_version`` advances by one)."""
    db = tmp_path / "stacks.db"
    conn = _build_at(db, 1)
    _seed(conn)
    for index in range(1, len(store._MIGRATIONS)):
        for statement in store._MIGRATIONS[index]:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {index + 1}")
    conn.close()

    s = StackStore(db)

    assert s.schema_version() == len(store._MIGRATIONS)
    # Version 1 had no link columns at all.
    assert s.get_links(1, "feat") == BranchLinks(None, None, None)
    assert s.get_parent(1, "plain") == "feat"


def test_an_empty_database_file_is_version_zero(tmp_path: Path):
    db = tmp_path / "stacks.db"
    sqlite3.connect(db).close()

    s = StackStore(db)

    assert s.schema_version() == len(store._MIGRATIONS)
    assert s.repos() == []
