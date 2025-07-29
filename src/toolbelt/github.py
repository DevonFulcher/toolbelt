from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx


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
                f"?q=is:pr+is:open+author:{username}+archived:false"
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
                )
                prs.append(pr)

            page += 1

    # Sort PRs by repository name
    return sorted(prs, key=lambda pr: pr.repo)
