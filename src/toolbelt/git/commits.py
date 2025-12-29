import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
    if "branch" not in columns:
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


def _get_commits_between_dates(
    start_date: date,
    end_date: date,
    *,
    org: str,
) -> list[Commit]:
    if end_date < start_date:
        return []

    with sqlite3.connect(str(_get_db_path())) as conn:
        _ensure_db(conn)
        cursor = conn.cursor()

        query = """
            SELECT message, repo_name, created_at, org, branch
            FROM commits
            WHERE date(created_at) BETWEEN ? AND ?
            AND org = ?
            ORDER BY created_at DESC
        """
        cursor.execute(query, [start_date.isoformat(), end_date.isoformat(), org])
        return [
            Commit(
                message, repo, datetime.fromisoformat(created_at), commit_org, branch
            )
            for message, repo, created_at, commit_org, branch in cursor.fetchall()
        ]


def _previous_standup_date(
    today: date,
    *,
    standup_weekdays: set[int],
) -> date:
    """
    Find the most recent standup date strictly before today.

    `standup_weekdays` uses Python's weekday numbering: Monday=0 ... Sunday=6.
    """
    if not standup_weekdays:
        raise ValueError("standup_weekdays must not be empty")

    for days_ago in range(1, 8):
        candidate = today - timedelta(days=days_ago)
        if candidate.weekday() in standup_weekdays:
            return candidate

    # If the set is non-empty, we should always find one within the last 7 days.
    raise RuntimeError("Failed to compute previous standup date")


def get_standup_date_window(
    standup_weekdays: set[int],
    *,
    now: datetime | None = None,
) -> tuple[date, date, date]:
    """
    Return a (start_date, end_date, previous_standup_date) window for standup-related
    reporting.

    This mirrors the window used by `get_recent_commits_for_standup`:
    - end_date: yesterday (today - 1)
    - start_date: the day after the most recent standup date strictly before today
    """
    now_utc = now or datetime.now(timezone.utc)
    today = now_utc.date()
    end_date = today - timedelta(days=1)
    if end_date < date.min:
        # Defensive: should never happen in practice.
        end_date = date.min

    prev_standup = _previous_standup_date(today, standup_weekdays=standup_weekdays)
    start_date = prev_standup + timedelta(days=1)
    return start_date, end_date, prev_standup


def get_recent_commits_for_standup(
    standup_weekdays: set[int],
    *,
    now: datetime | None = None,
) -> list[Commit]:
    """
    Get commits since the previous standup day (inclusive date range), excluding today.

    Example (Tue/Thu standups):
    - On Tuesday: includes Fri..Mon
    - On Thursday: includes Wed
    """
    current_org = os.getenv("CURRENT_ORG")
    if not current_org:
        raise ValueError("CURRENT_ORG environment variable must be set")

    start_date, end_date, _prev_standup = get_standup_date_window(
        standup_weekdays, now=now
    )
    if end_date < start_date:
        return []

    return _get_commits_between_dates(
        start_date,
        end_date,
        org=current_org.replace("_", "-"),
    )
