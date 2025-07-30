import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI


@dataclass
class Commit:
    message: str
    repo_name: str
    created_at: datetime
    org: str


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


def store_commit(message: str, repo_name: str, org: str) -> None:
    """Store a commit message in the database.

    Args:
        message: The commit message to store
        repo_name: The name of the repository the commit belongs to
    """
    with sqlite3.connect(str(_get_db_path())) as conn:
        _ensure_db(conn)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO commits (message, repo_name, org, created_at) VALUES (?, ?, ?, ?)",
            (
                message,
                repo_name,
                org,
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
            SELECT message, repo_name, created_at, org
            FROM commits
            WHERE date(created_at) = date('now', '-1 day')
            AND org = ?
            ORDER BY created_at DESC
        """

        cursor.execute(query, [current_org])
        return [
            Commit(message, repo, datetime.fromisoformat(created_at), org)
            for message, repo, created_at, org in cursor.fetchall()
        ]


def summarize_commits(commits: list[Commit]) -> str:
    """Summarize a list of commits using OpenAI's API.

    Args:
        commits: List of commits to summarize

    Returns:
        A bullet-point summary of the commits
    """
    if not commits:
        return ""

    # Format commits into a readable list
    commit_text = "\n\n".join(
        f"* org: {c.org}\nrepo: {c.repo_name}\nmessage: {c.message}" for c in commits
    )

    # Create the prompt
    prompt = f"""Summarize these git commits into 1-3 clear bullet points that explain the key changes:

{commit_text}

Format the response as bullet points starting with '*'. Focus on the main themes and group related changes together."""

    return (
        OpenAI()
        .responses.create(
            model="gpt-4o-mini",
            input=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that summarizes git commits into clear, concise bullet points.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        .output_text
    )
