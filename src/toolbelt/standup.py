import os
import textwrap
import webbrowser

import pyperclip  # type: ignore[import-untyped]

from toolbelt.git.commits import get_recent_commits_for_standup
from toolbelt.github import get_open_pull_requests
from toolbelt.logger import logger


def parse_standup_weekdays(raw: str) -> set[int]:
    """
    Parse standup weekdays from a comma-separated string of weekday abbreviations.

    Example: "tue,thu"

    Uses Python weekday numbering: Monday=0 ... Sunday=6.
    """
    normalized = raw.strip().lower().replace(" ", "")
    if not normalized:
        raise ValueError("Standup days must not be empty (example: 'tue,thu')")

    by_name: dict[str, int] = {
        "mon": 0,
        "tue": 1,
        "wed": 2,
        "thu": 3,
        "fri": 4,
        "sat": 5,
        "sun": 6,
    }

    tokens = [t for t in normalized.split(",") if t]
    if not tokens:
        raise ValueError("Standup days must not be empty (example: 'tue,thu')")

    unknown = sorted({t for t in tokens if t not in by_name})
    if unknown:
        allowed = ",".join(sorted(by_name.keys()))
        raise ValueError(
            f"Invalid standup day(s): {', '.join(unknown)}. Use: {allowed}"
        )

    return {by_name[t] for t in tokens}


def standup_notes(*, standup_weekdays: set[int]) -> None:
    """Prepare notes for standup and copy them to clipboard"""
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
    assert username and token
    open_prs = get_open_pull_requests(username, token)
    prs_text = "\n".join(
        [f"* {pr.title}: {pr.url} (Created {pr.time_open} ago)" for pr in open_prs]
    )
    recent_commits = get_recent_commits_for_standup(standup_weekdays)
    commits_section = ""
    if recent_commits:
        commits_text = "\n".join(
            [f"* {c.repo_name}: {c.message}" for c in recent_commits]
        )
        commits_section = f"\nRecent Changes\n{commits_text}"

    standup_text = (
        textwrap.dedent("""
        Yesterday
        *
        Today
        *
        Blockers
        * None
        """).strip()
        + commits_section
        + (f"\nOpen PRs\n{prs_text}" if prs_text else "")
    )
    logger.info(standup_text)
    pyperclip.copy(standup_text)
    webbrowser.open(
        "https://www.notion.so/dbtlabs/Devon-Fulcher-413bb38ebda783a0b19e8180994322fe"
    )
