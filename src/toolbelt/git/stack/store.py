"""SQLite persistence for stack metadata (which branch is stacked on which).

One database holds every repo toolbelt knows about, so a future UI and
multi-repo views can read it without touching each repo's git config.

Schema versioning uses ``PRAGMA user_version`` plus the ordered ``_MIGRATIONS``
list below: migration ``i`` takes the database from version ``i`` to ``i + 1``.
To change the schema, append a migration — never edit an existing one.

Several agents write concurrently, so connections use WAL + a busy timeout and
every multi-statement change runs in a ``BEGIN IMMEDIATE`` transaction.

This module is pure sqlite: it knows nothing about git. Callers resolve a
repo's identity (see ``lineage``) and pass it in.
"""

import contextlib
import os
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR_ENV_VAR = "TOOLBELT_HOME"
DB_FILENAME = "stacks.db"
_BUSY_TIMEOUT_MS = 30_000

Parents = dict[str, str]

_MIGRATIONS: list[tuple[str, ...]] = [
    (
        # A repo is identified by its canonical git common dir, which is shared
        # by all of its worktrees. ``imported_at`` is set once the legacy
        # ``toolbelt-stack.*`` git config was copied in.
        """
        CREATE TABLE repos (
            id INTEGER PRIMARY KEY,
            git_common_dir TEXT NOT NULL UNIQUE,
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            remote_url TEXT,
            imported_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        # ``base`` is reserved (nullable, currently unset). Later versions add
        # task / Claude-session / Jira link columns via new migrations.
        """
        CREATE TABLE branches (
            id INTEGER PRIMARY KEY,
            repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            parent TEXT NOT NULL,
            base TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (repo_id, name)
        )
        """,
    ),
]


@dataclass(frozen=True)
class RepoIdentity:
    git_common_dir: Path
    path: Path
    remote_url: str | None


@dataclass(frozen=True)
class RepoRecord:
    id: int
    name: str
    path: Path
    git_common_dir: Path


def state_dir() -> Path:
    """toolbelt's state dir: ``$TOOLBELT_HOME`` or ``~/.toolbelt``."""
    override = os.environ.get(STATE_DIR_ENV_VAR)
    return Path(override) if override else Path.home() / ".toolbelt"


def default_db_path() -> Path:
    return state_dir() / DB_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StackStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, transactions are explicit.
        self._conn = sqlite3.connect(
            db_path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        if self.schema_version() == len(_MIGRATIONS):
            return
        with self._write() as conn:
            # Re-read inside the write lock: another process may have migrated.
            for index in range(self.schema_version(), len(_MIGRATIONS)):
                for statement in _MIGRATIONS[index]:
                    conn.execute(statement)
                # PRAGMA can't take bound parameters.
                conn.execute(f"PRAGMA user_version = {index + 1}")

    def ensure_repo(
        self, identity: RepoIdentity, load_legacy_parents: Callable[[], Parents]
    ) -> int:
        """Return the repo's id, registering it (and, the first time it is
        seen, importing ``load_legacy_parents()``) as needed.

        Legacy rows never overwrite rows already in the database.
        """
        row = self._conn.execute(
            "SELECT id, imported_at FROM repos WHERE git_common_dir = ?",
            (str(identity.git_common_dir),),
        ).fetchone()
        if row is not None and row[1] is not None:
            return row[0]

        with self._write() as conn:
            # Re-check under the write lock so concurrent first uses import once.
            row = conn.execute(
                "SELECT id, imported_at FROM repos WHERE git_common_dir = ?",
                (str(identity.git_common_dir),),
            ).fetchone()
            now = _now()
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO repos "
                    "(git_common_dir, path, name, remote_url, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(identity.git_common_dir),
                        str(identity.path),
                        identity.path.name,
                        identity.remote_url,
                        now,
                    ),
                )
                assert cursor.lastrowid is not None
                repo_id: int = cursor.lastrowid
            else:
                repo_id = row[0]
                if row[1] is not None:
                    return repo_id
            for branch, parent in load_legacy_parents().items():
                conn.execute(
                    "INSERT OR IGNORE INTO branches "
                    "(repo_id, name, parent, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (repo_id, branch, parent, now, now),
                )
            conn.execute(
                "UPDATE repos SET imported_at = ? WHERE id = ?", (now, repo_id)
            )
            return repo_id

    def get_parent(self, repo_id: int, branch: str) -> str | None:
        row = self._conn.execute(
            "SELECT parent FROM branches WHERE repo_id = ? AND name = ?",
            (repo_id, branch),
        ).fetchone()
        return row[0] if row else None

    def set_parent(self, repo_id: int, branch: str, parent: str) -> None:
        now = _now()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO branches "
                "(repo_id, name, parent, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (repo_id, name) DO UPDATE SET "
                "parent = excluded.parent, updated_at = excluded.updated_at",
                (repo_id, branch, parent, now, now),
            )

    def remove_branch(self, repo_id: int, branch: str) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM branches WHERE repo_id = ? AND name = ?",
                (repo_id, branch),
            )

    def all_parents(self, repo_id: int) -> Parents:
        rows = self._conn.execute(
            "SELECT name, parent FROM branches WHERE repo_id = ?", (repo_id,)
        )
        return dict(rows.fetchall())

    def repos(self) -> list[RepoRecord]:
        rows = self._conn.execute(
            "SELECT id, name, path, git_common_dir FROM repos ORDER BY name, path"
        )
        return [
            RepoRecord(id=r[0], name=r[1], path=Path(r[2]), git_common_dir=Path(r[3]))
            for r in rows
        ]
