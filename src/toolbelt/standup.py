import os
import webbrowser
from datetime import datetime, time, timezone

from toolbelt.env_var import get_git_projects_workdir
from toolbelt.git.commits import (
    get_recent_commits_for_standup,
    get_standup_date_window,
)
from toolbelt.github import get_open_pull_requests
from toolbelt.linear import (
    LinearGraphQLError,
    get_in_progress_issues_with_changes_since,
)
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

    start_date, _end_date, _previous_standup = get_standup_date_window(standup_weekdays)
    since = datetime.combine(start_date, time.min, tzinfo=timezone.utc)

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

    linear_section = ""
    linear_api_key = os.getenv("LINEAR_API_KEY")
    if linear_api_key:
        try:
            in_progress = get_in_progress_issues_with_changes_since(
                api_key=linear_api_key,
                since=since,
            )
        except LinearGraphQLError as e:
            logger.error(f"Failed to fetch Linear issues: {e}")
            in_progress = []

        if in_progress:
            lines: list[str] = []
            for issue in in_progress:
                lines.append(f"* {issue.identifier}: {issue.title} ({issue.url})")
                if not issue.changes:
                    lines.append("  * (No changes since last standup)")
                    continue

                for change in issue.changes:
                    actor = f" ({change.actor})" if change.actor else ""
                    when = change.created_at.astimezone(timezone.utc).strftime(
                        "%Y-%m-%d"
                    )
                    detail = change.type or "updated"
                    if change.from_value and change.to_value:
                        detail = f"{detail}: {change.from_value} -> {change.to_value}"
                    elif change.data:
                        detail = f"{detail}: {change.data}"
                    lines.append(f"  * {when}: {detail}{actor}")

            linear_section = "\nLinear (In Progress)\n" + "\n".join(lines)
    else:
        logger.warning("No Linear API key found")

    standup_text = (
        commits_section
        + linear_section
        + (f"\nOpen PRs\n{prs_text}" if prs_text else "")
    )
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    year = now.strftime("%Y")
    month = now.strftime("%m")
    techdocs_repo = get_git_projects_workdir() / "tech-docs"
    if not techdocs_repo.exists():
        logger.error("Techdocs repository not found")
        return
    standup_file = techdocs_repo / "standups" / year / month / f"{today}.md"
    standup_file.parent.mkdir(parents=True, exist_ok=True)
    standup_file.write_text(standup_text)
    logger.info(standup_text)
    webbrowser.open(
        "https://www.notion.so/dbtlabs/Devon-Fulcher-413bb38ebda783a0b19e8180994322fe"
    )
