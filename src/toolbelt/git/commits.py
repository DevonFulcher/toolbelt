import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path



@dataclass
class Commit:
    message: str
    repo_name: str
    created_at: datetime
    org: str
    branch: str


def _get_db_path() -> Path:
    """Get the path to the database file."""
    toolbelt_dir = Path.home() / ".toolbelt"
    toolbelt_dir.mkdir(parents=True, exist_ok=True)
    return toolbelt_dir / "storage.db"


def _ensure_db(conn: sqlite3.Connection) -> None:
    """Ensure the database schema exists."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS commits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            org TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)

    # Migration: Add branch column if it doesn't exist
    cursor.execute("PRAGMA table_info(commits)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'branch' not in columns:
        cursor.execute("ALTER TABLE commits ADD COLUMN branch TEXT DEFAULT 'main'")


def store_commit(message: str, repo_name: str, org: str, branch: str) -> None:
    """Store a commit message in the database.

    Args:
        message: The commit message to store
        repo_name: The name of the repository the commit belongs to
        org: The organization name
        branch: The branch name
    """
    with sqlite3.connect(str(_get_db_path())) as conn:
        _ensure_db(conn)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO commits (message, repo_name, org, branch, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                message,
                repo_name,
                org,
                branch,
                datetime.now(timezone.utc),
            ),
        )


def get_yesterdays_commits() -> list[Commit]:
    """Get all commits from yesterday for the current organization.

    Returns:
        List of Commit objects from yesterday for CURRENT_ORG

    Raises:
        ValueError: If CURRENT_ORG environment variable is not set
    """
    current_org = os.getenv("CURRENT_ORG")
    if not current_org:
        raise ValueError("CURRENT_ORG environment variable must be set")

    with sqlite3.connect(str(_get_db_path())) as conn:
        _ensure_db(conn)
        cursor = conn.cursor()

        query = """
            SELECT message, repo_name, created_at, org, branch
            FROM commits
            WHERE date(created_at) = CASE
                -- If it's Monday (weekday 1), get Friday's commits (3 days ago)
                WHEN strftime('%w', 'now') = '1' THEN date('now', '-3 days')
                -- Otherwise get yesterday's commits
                ELSE date('now', '-1 day')
            END
            AND org = ?
            ORDER BY created_at DESC
        """

        cursor.execute(query, [current_org.replace("_", "-")])
        return [
            Commit(message, repo, datetime.fromisoformat(created_at), org, branch)
            for message, repo, created_at, org, branch in cursor.fetchall()
        ]
