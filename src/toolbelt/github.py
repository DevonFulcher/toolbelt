from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from toolbelt.logger import logger


@dataclass
class TimeOpen:
    days: int
    hours: int
    minutes: int

    @classmethod
    def from_timedelta(cls, delta: timedelta) -> "TimeOpen":
        total_minutes = int(delta.total_seconds() / 60)
        days = total_minutes // (24 * 60)
        remaining_minutes = total_minutes % (24 * 60)
        hours = remaining_minutes // 60
        minutes = remaining_minutes % 60
        return cls(days=days, hours=hours, minutes=minutes)

    def __str__(self) -> str:
        if self.days == 0:
            if self.hours == 0:
                return f"{self.minutes}m"
            return f"{self.hours}h"
        return f"{self.days}d {self.hours}h"


@dataclass
class PullRequest:
    title: str
    url: str
    repo: str
    created_at: datetime
    time_open: TimeOpen
    number: int
    state: str
    draft: bool
    mergeable_state: Optional[str] = None
    review_decision: Optional[str] = None
    ci_status: Optional[str] = None


def get_open_pull_requests(username: str, token: str) -> list[PullRequest]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }

    prs: list[PullRequest] = []
    page = 1

    with httpx.Client(headers=headers, timeout=10.0) as client:
        while True:
            url = (
                "https://api.github.com/search/issues"
                f"?q=is:pr+is:open+author:{username}+archived:false+-draft:true+-review:approved"
                f"&per_page=100&page={page}"
            )
            response = client.get(url)
            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                # Extract repo from URL (e.g., https://github.com/owner/repo/pull/123)
                html_url = item["html_url"]
                repo_path = html_url.split("github.com/")[1].split("/pull/")[0]

                # Parse created_at timestamp and calculate time open
                created_at = datetime.fromisoformat(
                    item["created_at"].replace("Z", "+00:00")
                )
                time_delta = datetime.now(timezone.utc) - created_at
                time_open = TimeOpen.from_timedelta(time_delta)

                pr = PullRequest(
                    title=item["title"],
                    url=html_url,
                    repo=repo_path,
                    created_at=created_at,
                    time_open=time_open,
                    number=item["number"],
                    state=item["state"],
                    draft=item.get("draft", False),
                )
                prs.append(pr)

            page += 1

    # Sort PRs by repository name
    return sorted(prs, key=lambda pr: pr.repo)


def get_pr_status(username: str, token: str) -> list[PullRequest]:
    """Get all PRs created by the user with detailed status information."""
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }

    prs: list[PullRequest] = []
    page = 1

    with httpx.Client(headers=headers, timeout=10.0) as client:
        while True:
            # Get all PRs created by the user (open and closed)
            url = (
                "https://api.github.com/search/issues"
                f"?q=is:pr+author:{username}+archived:false"
                f"&per_page=100&page={page}&sort=updated"
            )
            response = client.get(url)
            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                # Extract repo from URL
                html_url = item["html_url"]
                repo_path = html_url.split("github.com/")[1].split("/pull/")[0]

                # Parse created_at timestamp and calculate time open
                created_at = datetime.fromisoformat(
                    item["created_at"].replace("Z", "+00:00")
                )
                time_delta = datetime.now(timezone.utc) - created_at
                time_open = TimeOpen.from_timedelta(time_delta)

                # Get detailed PR information
                pr_details = get_pr_details(client, repo_path, item["number"])

                pr = PullRequest(
                    title=item["title"],
                    url=html_url,
                    repo=repo_path,
                    created_at=created_at,
                    time_open=time_open,
                    number=item["number"],
                    state=item["state"],
                    draft=item.get("draft", False),
                    mergeable_state=pr_details.get("mergeable_state"),
                    review_decision=pr_details.get("review_decision"),
                    ci_status=pr_details.get("ci_status"),
                )
                prs.append(pr)

            page += 1

    # Sort PRs by repository name
    return sorted(prs, key=lambda pr: pr.repo)


def get_pr_details(client: httpx.Client, repo: str, pr_number: int) -> dict:
    """Get detailed information about a specific PR."""
    try:
        # Get PR details
        pr_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
        pr_response = client.get(pr_url)
        pr_response.raise_for_status()
        pr_data = pr_response.json()

        # Get check runs for CI status
        checks_url = f"https://api.github.com/repos/{repo}/commits/{pr_data['head']['sha']}/check-runs"
        checks_response = client.get(checks_url)
        checks_data = (
            checks_response.json()
            if checks_response.status_code == 200
            else {"check_runs": []}
        )

        # Determine CI status
        ci_status = "unknown"
        if checks_data.get("check_runs"):
            check_runs = checks_data["check_runs"]
            if all(
                run["conclusion"] == "success"
                for run in check_runs
                if run.get("conclusion")
            ):
                ci_status = "success"
            elif any(
                run["conclusion"] == "failure"
                for run in check_runs
                if run.get("conclusion")
            ):
                ci_status = "failure"
            elif any(run["status"] == "in_progress" for run in check_runs):
                ci_status = "in_progress"
            elif any(
                run["conclusion"] == "pending"
                for run in check_runs
                if run.get("conclusion")
            ):
                ci_status = "pending"

        return {
            "mergeable_state": pr_data.get("mergeable_state"),
            "review_decision": pr_data.get("review_decision"),
            "ci_status": ci_status,
        }
    except Exception:
        return {}


def get_review_requests(username: str, token: str) -> list[PullRequest]:
    """Get PRs where the user's review is requested."""
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }

    prs: list[PullRequest] = []
    page = 1

    with httpx.Client(headers=headers, timeout=10.0) as client:
        while True:
            # Search for PRs where user is requested as reviewer
            url = (
                "https://api.github.com/search/issues"
                f"?q=is:pr+is:open+review-requested:{username}+archived:false"
                f"&per_page=100&page={page}"
            )
            response = client.get(url)
            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                # Extract repo from URL
                html_url = item["html_url"]
                repo_path = html_url.split("github.com/")[1].split("/pull/")[0]

                # Parse created_at timestamp and calculate time open
                created_at = datetime.fromisoformat(
                    item["created_at"].replace("Z", "+00:00")
                )
                time_delta = datetime.now(timezone.utc) - created_at
                time_open = TimeOpen.from_timedelta(time_delta)

                # Get detailed PR information
                pr_details = get_pr_details(client, repo_path, item["number"])

                pr = PullRequest(
                    title=item["title"],
                    url=html_url,
                    repo=repo_path,
                    created_at=created_at,
                    time_open=time_open,
                    number=item["number"],
                    state=item["state"],
                    draft=item.get("draft", False),
                    mergeable_state=pr_details.get("mergeable_state"),
                    review_decision=pr_details.get("review_decision"),
                    ci_status=pr_details.get("ci_status"),
                )
                prs.append(pr)

            page += 1

    # Sort PRs by repository name
    return sorted(prs, key=lambda pr: pr.repo)


def display_status(username: str, token: str) -> None:
    """Display GitHub PR status and review requests."""
    logger.info("🔍 Fetching your PRs and review requests...")

    # Get PRs created by user
    my_prs = get_pr_status(username, token)

    # Get PRs where review is requested
    review_requests = get_review_requests(username, token)

    # Display created PRs
    if my_prs:
        logger.info(f"\n📝 Your PRs ({len(my_prs)}):")
        logger.info("=" * 80)

        for pr in my_prs:
            status_icons = []
            if pr.draft:
                status_icons.append("📝 draft")
            if pr.state == "closed":
                status_icons.append("✅ closed")
            elif pr.state == "open":
                if pr.review_decision == "approved":
                    status_icons.append("✅ approved")
                elif pr.review_decision == "changes_requested":
                    status_icons.append("🔄 changes requested")
                elif pr.review_decision == "commented":
                    status_icons.append("💬 commented")
                else:
                    status_icons.append("⏳ pending review")

            if pr.ci_status:
                ci_icon = {
                    "success": "✅",
                    "failure": "❌",
                    "in_progress": "🔄",
                    "pending": "⏳",
                }.get(pr.ci_status, "❓")
                status_icons.append(f"{ci_icon} CI: {pr.ci_status}")

            status_text = " | ".join(status_icons) if status_icons else "No status"

            logger.info(f"#{pr.number} {pr.title}")
            logger.info(f"  📁 {pr.repo}")
            logger.info(f"  🔗 {pr.url}")
            logger.info(f"  📊 {status_text}")
            logger.info(f"  ⏰ Created {pr.time_open} ago")
            logger.info("")
    else:
        logger.info("\n📝 No PRs found")

    # Display review requests
    if review_requests:
        logger.info(f"\n👀 Review Requests ({len(review_requests)}):")
        logger.info("=" * 80)

        for pr in review_requests:
            status_icons = []
            if pr.draft:
                status_icons.append("📝 draft")

            if pr.ci_status:
                ci_icon = {
                    "success": "✅",
                    "failure": "❌",
                    "in_progress": "🔄",
                    "pending": "⏳",
                }.get(pr.ci_status, "❓")
                status_icons.append(f"{ci_icon} CI: {pr.ci_status}")

            status_text = " | ".join(status_icons) if status_icons else "No status"

            logger.info(f"#{pr.number} {pr.title}")
            logger.info(f"  📁 {pr.repo}")
            logger.info(f"  🔗 {pr.url}")
            logger.info(f"  📊 {status_text}")
            logger.info(f"  ⏰ Created {pr.time_open} ago")
            logger.info("")
    else:
        logger.info("\n👀 No review requests found")
