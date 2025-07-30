import os
import textwrap
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
        [
            f"        * {pr.title}: {pr.url} (Created {pr.time_open} ago)"
            for pr in open_prs
        ]
    )
    yesterday_commits = get_yesterdays_commits()
    print(yesterday_commits)
    commit_summary = summarize_commits(yesterday_commits)
    standup_text = (
        Template(
            textwrap.dedent(f"""
        Yesterday
        $commit_summary
        Today
        *
        Blockers
        * None
        Open PRs\n{prs_text}
        """)
        )
        .substitute(commit_summary=commit_summary)
        .strip()
    )
    print(standup_text)
    pyperclip.copy(standup_text)
