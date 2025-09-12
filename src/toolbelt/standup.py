import os
import textwrap
import webbrowser
from string import Template

import pyperclip  # type: ignore[import-untyped]

from toolbelt.git.commits import get_yesterdays_commits, summarize_commits
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
    commit_summary = summarize_commits(yesterday_commits)
    standup_text = (
        (
            Template(
                textwrap.dedent("""
        Yesterday
        $commit_summary
        Today
        *
        Blockers
        * None
        """)
            ).substitute(commit_summary=commit_summary)
        )
        + (f"Open PRs\n{prs_text}" if prs_text else "")
    ).strip()
    print(standup_text)
    pyperclip.copy(standup_text)
    webbrowser.open(
        "https://www.notion.so/dbtlabs/Devon-Fulcher-413bb38ebda783a0b19e8180994322fe"
    )
