import os
import textwrap

import pyperclip  # type: ignore[import-untyped]

from toolbelt.github import get_open_pull_requests


def standup_notes() -> None:
    """Prepare notes for standup and copy them to clipboard"""
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
    assert username and token
    open_prs = get_open_pull_requests(username, token)
    prs_text = "\n".join(
        [f"    * {pr.url} (Open for {pr.time_open})" for pr in open_prs]
    )
    standup_text = textwrap.dedent(f"""
    Yesterday
    *
    Today
    *
    Blockers & Open PRs\n{prs_text}
    """)
    print(standup_text)
    pyperclip.copy(standup_text)
