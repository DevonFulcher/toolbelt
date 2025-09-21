import os
import textwrap
import webbrowser

import pyperclip  # type: ignore[import-untyped]

from toolbelt.git.commits import get_yesterdays_commits
from toolbelt.github import get_open_pull_requests


def standup_notes() -> None:
    """Prepare notes for standup and copy them to clipboard"""
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
    assert username and token
    open_prs = get_open_pull_requests(username, token)
    prs_text = "\n".join(
        [f"* {pr.title}: {pr.url} (Created {pr.time_open} ago)" for pr in open_prs]
    )
    yesterday_commits = get_yesterdays_commits()
    commits_section = ""
    if yesterday_commits:
        commits_text = "\n".join(
            [f"* {c.repo_name} ({c.branch}): {c.message}" for c in yesterday_commits]
        )
        commits_section = f"\nRecent Commits\n{commits_text}"

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
    print(standup_text)
    pyperclip.copy(standup_text)
    webbrowser.open(
        "https://www.notion.so/dbtlabs/Devon-Fulcher-413bb38ebda783a0b19e8180994322fe"
    )
