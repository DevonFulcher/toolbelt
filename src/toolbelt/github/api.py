from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from toolbelt.github.client import GithubClient, build_async_github_client


class CheckRunConclusion(str, Enum):
    # Action is required to address a failing check.
    ACTION_REQUIRED = "action_required"
    # Check was cancelled before completing.
    CANCELLED = "cancelled"
    # Check completed with a failure.
    FAILURE = "failure"
    # Check completed with a neutral result.
    NEUTRAL = "neutral"
    # Check was skipped entirely.
    SKIPPED = "skipped"
    # Check result is stale (superseded).
    STALE = "stale"
    # Check completed successfully.
    SUCCESS = "success"
    # Check timed out before finishing.
    TIMED_OUT = "timed_out"


class CheckRunStatus(str, Enum):
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    QUEUED = "queued"


class CIStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    IN_PROGRESS = "in_progress"
    PENDING = "pending"
    UNKNOWN = "unknown"


class PullRequestState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class PullRequestMergeableState(str, Enum):
    CLEAN = "clean"
    DIRTY = "dirty"
    UNKNOWN = "unknown"
    UNSTABLE = "unstable"
    BLOCKED = "blocked"
    BEHIND = "behind"
    HAS_HOOKS = "has_hooks"


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    REVIEW_REQUIRED = "review_required"


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
    state: PullRequestState
    draft: bool
    mergeable_state: Optional[PullRequestMergeableState] = None
    review_decision: Optional[ReviewDecision] = None
    ci_status: Optional[CIStatus] = None


class GithubUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    login: str


class PullRequestLink(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str


def _repo_full_name_from_url(url: str) -> str:
    min_repo_parts = 2
    if "/repos/" in url:
        return url.split("/repos/")[-1]
    parts = url.strip("/").split("/")
    if len(parts) >= min_repo_parts:
        return "/".join(parts[-min_repo_parts:])
    return url


class SearchIssueItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    html_url: str
    repository_url: str
    pull_request: PullRequestLink
    number: int

    @computed_field(return_type=str)
    def repo_full_name(self) -> str:
        return _repo_full_name_from_url(self.repository_url)


class SearchIssueItemDetailed(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    html_url: str
    repository_url: str
    pull_request: PullRequestLink
    number: int
    state: PullRequestState
    draft: bool = False
    created_at: str

    @computed_field(return_type=str)
    def repo_full_name(self) -> str:
        return _repo_full_name_from_url(self.repository_url)


class SearchIssuesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SearchIssueItem] = Field(default_factory=list)


class SearchIssuesResponseDetailed(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SearchIssueItemDetailed] = Field(default_factory=list)


class PullRequestHead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: str


class PullRequestRepo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str


class PullRequestBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repo: PullRequestRepo


class PullRequestDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mergeable_state: Optional[PullRequestMergeableState] = None
    review_decision: Optional[ReviewDecision] = None
    head: PullRequestHead
    base: PullRequestBase


class ReviewState(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    DISMISSED = "dismissed"
    PENDING = "pending"


class Review(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    state: ReviewState
    user: GithubUser

    @field_validator("state", mode="before")
    @classmethod
    def _normalize_state(cls, value: object) -> object:
        if isinstance(value, str):
            return value.lower()
        return value


class IssueComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    user: GithubUser
    html_url: str
    url: str
    body: str


class ReviewComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    pull_request_review_id: Optional[int] = None
    in_reply_to_id: Optional[int] = None
    user: GithubUser
    html_url: str
    url: str


class CheckRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    conclusion: Optional[CheckRunConclusion] = None
    status: CheckRunStatus


class CheckRunsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    check_runs: list[CheckRun] = Field(default_factory=list)


def extract_repo_from_pr_url(html_url: str) -> str:
    return html_url.split("github.com/")[1].split("/pull/")[0]


async def search_open_authored_prs(
    client: GithubClient,
    username: str,
) -> list[SearchIssueItem]:
    async def fetch_page(page: int) -> list[SearchIssueItem]:
        url = (
            "https://api.github.com/search/issues"
            f"?q=is:pr+is:open+author:{username}+archived:false+-draft:true"
            f"&per_page=100&page={page}&sort=updated"
        )
        payload = await client.get_model(url, SearchIssuesResponse)
        return payload.items

    return await client._paginate(fetch_page)


async def get_open_pull_requests(username: str, token: str) -> list[PullRequest]:
    prs: list[PullRequest] = []
    page = 1
    async with build_async_github_client(token) as client:
        while True:
            url = (
                "https://api.github.com/search/issues"
                f"?q=is:pr+is:open+author:{username}+archived:false+-draft:true+-review:approved"
                f"&per_page=100&page={page}"
            )
            payload = await client.get_model(url, SearchIssuesResponseDetailed)
            items = payload.items
            if not items:
                break

            for item in items:
                detailed = item
                repo_path = extract_repo_from_pr_url(detailed.html_url)

                created_at = datetime.fromisoformat(
                    detailed.created_at.replace("Z", "+00:00")
                )
                time_delta = datetime.now(timezone.utc) - created_at
                time_open = TimeOpen.from_timedelta(time_delta)

                pr = PullRequest(
                    title=detailed.title,
                    url=detailed.html_url,
                    repo=repo_path,
                    created_at=created_at,
                    time_open=time_open,
                    number=detailed.number,
                    state=detailed.state,
                    draft=detailed.draft,
                )
                prs.append(pr)

            page += 1

    return sorted(prs, key=lambda pr: pr.repo)


async def get_pr_status(username: str, token: str) -> list[PullRequest]:
    prs: list[PullRequest] = []
    page = 1

    async with build_async_github_client(token) as client:
        while True:
            url = (
                "https://api.github.com/search/issues"
                f"?q=is:pr+author:{username}+archived:false"
                f"&per_page=100&page={page}&sort=updated"
            )
            payload = await client.get_model(url, SearchIssuesResponseDetailed)
            items = payload.items
            if not items:
                break

            for item in items:
                detailed = item
                repo_path = extract_repo_from_pr_url(detailed.html_url)
                repo_api_prefix = f"https://api.github.com/repos/{repo_path}"

                created_at = datetime.fromisoformat(
                    detailed.created_at.replace("Z", "+00:00")
                )
                time_delta = datetime.now(timezone.utc) - created_at
                time_open = TimeOpen.from_timedelta(time_delta)

                pr_details = await client.get_model(
                    f"{repo_api_prefix}/pulls/{detailed.number}",
                    PullRequestDetails,
                )
                head_sha = pr_details.head.sha
                ci_status = None
                if head_sha:
                    try:
                        checks_payload = await client.get_model(
                            f"{repo_api_prefix}/commits/{head_sha}/check-runs",
                            CheckRunsResponse,
                        )
                    except httpx.HTTPStatusError:
                        checks_payload = None
                    if checks_payload:
                        ci_status = summarize_ci_status(checks_payload.check_runs)

                pr = PullRequest(
                    title=detailed.title,
                    url=detailed.html_url,
                    repo=repo_path,
                    created_at=created_at,
                    time_open=time_open,
                    number=detailed.number,
                    state=detailed.state,
                    draft=detailed.draft,
                    mergeable_state=pr_details.mergeable_state,
                    review_decision=pr_details.review_decision,
                    ci_status=ci_status,
                )
                prs.append(pr)

            page += 1

    return sorted(prs, key=lambda pr: pr.repo)


async def get_review_requests(username: str, token: str) -> list[PullRequest]:
    prs: list[PullRequest] = []
    page = 1

    async with build_async_github_client(token) as client:
        while True:
            url = (
                "https://api.github.com/search/issues"
                f"?q=is:pr+is:open+review-requested:{username}+archived:false"
                f"&per_page=100&page={page}"
            )
            payload = await client.get_model(url, SearchIssuesResponseDetailed)
            items = payload.items
            if not items:
                break

            for item in items:
                detailed = item
                repo_path = extract_repo_from_pr_url(detailed.html_url)
                repo_api_prefix = f"https://api.github.com/repos/{repo_path}"

                created_at = datetime.fromisoformat(
                    detailed.created_at.replace("Z", "+00:00")
                )
                time_delta = datetime.now(timezone.utc) - created_at
                time_open = TimeOpen.from_timedelta(time_delta)

                pr_details = await client.get_model(
                    f"{repo_api_prefix}/pulls/{detailed.number}",
                    PullRequestDetails,
                )
                head_sha = pr_details.head.sha
                ci_status = None
                if head_sha:
                    try:
                        checks_payload = await client.get_model(
                            f"{repo_api_prefix}/commits/{head_sha}/check-runs",
                            CheckRunsResponse,
                        )
                    except httpx.HTTPStatusError:
                        checks_payload = None
                    if checks_payload:
                        ci_status = summarize_ci_status(checks_payload.check_runs)

                pr = PullRequest(
                    title=detailed.title,
                    url=detailed.html_url,
                    repo=repo_path,
                    created_at=created_at,
                    time_open=time_open,
                    number=detailed.number,
                    state=detailed.state,
                    draft=detailed.draft,
                    mergeable_state=pr_details.mergeable_state,
                    review_decision=pr_details.review_decision,
                    ci_status=ci_status,
                )
                prs.append(pr)

            page += 1

    return sorted(prs, key=lambda pr: pr.repo)


def summarize_ci_status(check_runs: list[CheckRun]) -> Optional[CIStatus]:
    if not check_runs:
        return None
    if all(
        run.conclusion == CheckRunConclusion.SUCCESS
        for run in check_runs
        if run.conclusion
    ):
        return CIStatus.SUCCESS
    if any(
        run.conclusion == CheckRunConclusion.FAILURE
        for run in check_runs
        if run.conclusion
    ):
        return CIStatus.FAILURE
    if any(run.status == CheckRunStatus.IN_PROGRESS for run in check_runs):
        return CIStatus.IN_PROGRESS
    if any(run.status == CheckRunStatus.QUEUED for run in check_runs):
        return CIStatus.PENDING
    return CIStatus.UNKNOWN
