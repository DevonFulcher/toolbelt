"""toolbelt's DB (``stacks.db``) as it travels: copied, reset, path-rewritten.

The DB is in WAL mode, so a plain file copy can miss or tear committed data;
sqlite's online backup API (what ``sqlite3 .backup`` uses) makes a consistent
copy. Both ends work on plain ``sqlite3`` connections rather than
``StackStore``: opening the store would migrate the copy.
"""

import json
import sqlite3
from pathlib import Path

from toolbelt.machine_move.archive_layout import MACHINE_SPECIFIC_TABLES, PATH_COLUMNS
from toolbelt.machine_move.path_prefixes import PrefixMap


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def backup(source: Path, dest: Path) -> None:
    """A consistent single-file copy of ``source`` (live or not) at ``dest``."""
    reader = sqlite3.connect(source)
    writer = sqlite3.connect(dest)
    try:
        reader.backup(writer)
        # One file in the archive, not a database that needs -wal and -shm.
        writer.execute("PRAGMA journal_mode = DELETE")
    finally:
        writer.close()
        reader.close()


def reset_for_export(path: Path) -> None:
    """Empty the machine-specific tables; a run still ``running`` cannot
    continue on another machine, so it ends as abandoned."""
    conn = sqlite3.connect(path)
    try:
        tables = _tables(conn)
        for table in MACHINE_SPECIFIC_TABLES:
            if table in tables:
                conn.execute(f"DELETE FROM {table}")
        if "agent_runs" in tables:
            conn.execute(
                "UPDATE agent_runs SET state = 'abandoned' WHERE state = 'running'"
            )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def rewrite_paths(path: Path, prefixes: PrefixMap) -> int:
    """Rewrite the old prefixes in ``PATH_COLUMNS``; returns the cells changed."""
    conn = sqlite3.connect(path)
    changed = 0
    try:
        tables = _tables(conn)
        for table, column, where in PATH_COLUMNS:
            if table not in tables or column not in _columns(conn, table):
                continue
            condition = f" WHERE {where}" if where else ""
            rows = conn.execute(
                f"SELECT rowid, {column} FROM {table}{condition}"
            ).fetchall()
            for rowid, value in rows:
                if not isinstance(value, str):
                    continue
                new = prefixes.path(value)
                if new != value:
                    conn.execute(
                        f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                        (new, rowid),
                    )
                    changed += 1
        conn.commit()
    finally:
        conn.close()
    return changed


def check_integrity(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        (result,) = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if result != "ok":
        raise ValueError(f"{path} failed its integrity check: {result}")


def has_user_data(path: Path) -> bool:
    """Whether the DB at ``path`` tracks any repo or branch (a DB that only
    has its schema, as a first ``tt`` run leaves, has none)."""
    conn = sqlite3.connect(path)
    try:
        tables = _tables(conn)
        return any(
            table in tables
            and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("repos", "branches")
        )
    finally:
        conn.close()


def blockers(path: Path) -> list[str]:
    """Why the DB should not be exported now: autonomous runs in flight, or
    decisions waiting for an answer."""
    conn = sqlite3.connect(path)
    reasons: list[str] = []
    try:
        tables = _tables(conn)
        if "agent_runs" in tables:
            for state, text in (
                ("running", "autonomous run(s) running"),
                ("deciding", "autonomous run(s) waiting for a decision"),
            ):
                (count,) = conn.execute(
                    "SELECT COUNT(*) FROM agent_runs WHERE state = ?", (state,)
                ).fetchone()
                if count:
                    reasons.append(f"{count} {text}")
        if "decisions" in tables:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE status = 'pending'"
            ).fetchone()
            if count:
                reasons.append(f"{count} decision(s) pending an answer")
    finally:
        conn.close()
    return reasons


def repo_rows(path: Path) -> list[tuple[str, str | None]]:
    """``(path, remote_url)`` of every registered repo."""
    conn = sqlite3.connect(path)
    try:
        if "repos" not in _tables(conn):
            return []
        return [
            (row[0], row[1])
            for row in conn.execute("SELECT path, remote_url FROM repos ORDER BY id")
        ]
    finally:
        conn.close()


def tracked_branches(path: Path) -> list[tuple[str, str]]:
    """``(repo path, branch)`` of every tracked branch."""
    conn = sqlite3.connect(path)
    try:
        if not {"repos", "branches"} <= _tables(conn):
            return []
        return [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT r.path, b.name FROM branches b "
                "JOIN repos r ON r.id = b.repo_id ORDER BY b.id"
            )
        ]
    finally:
        conn.close()


def pr_states(path: Path) -> dict[tuple[str, str], str]:
    """The last looked-up PR state (``open``, ``draft``, ``merged``,
    ``closed``) of each tracked branch, by ``(repo path, branch)``. Call it
    before ``reset_for_export``, which empties the lookups."""
    conn = sqlite3.connect(path)
    try:
        if not {"repos", "pr_lookups"} <= _tables(conn):
            return {}
        found: dict[tuple[str, str], str] = {}
        for repo_path, branch, record in conn.execute(
            "SELECT r.path, l.branch, l.record FROM pr_lookups l "
            "JOIN repos r ON r.id = l.repo_id"
        ):
            state = (json.loads(record).get("status") or {}).get("pr")
            if state:
                found[(repo_path, branch)] = state
        return found
    finally:
        conn.close()
