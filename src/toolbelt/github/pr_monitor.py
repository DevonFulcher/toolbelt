from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from toolbelt.github.api import (
    CheckRun,
    CheckRunsResponse,
    IssueComment,
    PullRequestDetails,
    PullRequestMergeableState,
    Review,
    ReviewComment,
    SearchIssueItem,
    search_open_authored_prs,
)
from toolbelt.github.client import (
    GithubClient,
    build_async_github_client,
)
from toolbelt.logger import logger


@dataclass
class PrState:
    mergeable_state: str | None = None
    review_decision: str | None = None
    check_runs: list[CheckRun] = field(default_factory=list)
    last_check_run_id: int = 0
    last_review_id: int = 0
    last_issue_comment_id: int = 0
    last_review_comment_id: int = 0


@dataclass
class ReviewWithComments:
    review: Review
    comments: list[ReviewComment]


class HasId(Protocol):
    id: int


@dataclass(frozen=True)
class PrRef:
    repo: str
    number: int

    @property
    def pr_url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


class PrMonitorHooks(ABC):
    @abstractmethod
    def on_merge_conflict(
        self,
        pr: PrRef,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def on_new_review(
        self,
        pr: PrRef,
        reviews: list[ReviewWithComments],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def on_new_issue_comment(
        self,
        pr: PrRef,
        comments: list[IssueComment],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def on_ci_status_change(
        self,
        pr: PrRef,
        check_run: CheckRun,
    ) -> None:
        raise NotImplementedError


class LoggingPrMonitorHooks(PrMonitorHooks):
    def on_merge_conflict(self, pr: PrRef) -> None:
        logger.info(
            "Merge conflict detected for %s (%s)",
            pr.repo,
            pr.number,
        )

    def on_new_review(
        self,
        pr: PrRef,
        reviews: list[ReviewWithComments],
    ) -> None:
        logger.info(
            "New reviews (%s reviews, %s comments)",
            len(reviews),
            sum(len(entry.comments) for entry in reviews),
        )

    def on_new_issue_comment(
        self,
        pr: PrRef,
        comments: list[IssueComment],
    ) -> None:
        logger.info(
            "New comments on (%s)",
            len(comments),
        )

    def on_ci_status_change(
        self,
        pr: PrRef,
        check_run: CheckRun,
    ) -> None:
        logger.info(
            "CI check run updated for: %s (%s)",
            check_run.id,
            pr.number,
        )


class PrMonitor:
    def __init__(
        self,
        client: GithubClient,
        username: str,
        hooks: PrMonitorHooks,
    ) -> None:
        self._client = client
        self._username = username
        self._hooks = hooks
        self._state: dict[str, PrState] = {}

    async def poll_once(self) -> None:
        prs = await search_open_authored_prs(
            self._client,
            self._username,
        )

        seen_keys: set[str] = set()
        for pr in prs:
            pr_key = await self._process_pr(pr)
            if pr_key:
                seen_keys.add(pr_key)

        stale_keys = set(self._state.keys()) - seen_keys
        for key in stale_keys:
            logger.debug("Removing stale PR state for %s", key)
            self._state.pop(key, None)

    async def _process_pr(self, issue_search_result: SearchIssueItem) -> str | None:
        pr_details = await self._client.get_model(
            issue_search_result.pull_request.url,
            PullRequestDetails,
        )

        number = issue_search_result.number
        repo = pr_details.base.repo.full_name
        repo_api_prefix = f"https://api.github.com/repos/{repo}"
        reviews = await self._client.paged_get(
            f"{repo_api_prefix}/pulls/{number}/reviews",
            Review,
        )
        issue_comments = await self._client.paged_get(
            f"{repo_api_prefix}/issues/{number}/comments",
            IssueComment,
        )
        review_comments = await self._client.paged_get(
            f"{repo_api_prefix}/pulls/{number}/comments",
            ReviewComment,
        )
        check_runs_payload = await self._client.get_model(
            f"{repo_api_prefix}/commits/{pr_details.head.sha}/check-runs",
            CheckRunsResponse,
        )
        check_runs = check_runs_payload.check_runs

        mergeable_state = pr_details.mergeable_state
        review_decision = pr_details.review_decision
        current_state = PrState(
            mergeable_state=mergeable_state,
            review_decision=review_decision,
            check_runs=check_runs,
            last_check_run_id=self._max_id(check_runs),
            last_review_id=self._max_id(reviews),
            last_issue_comment_id=self._max_id(issue_comments),
            last_review_comment_id=self._max_id(review_comments),
        )

        pr_key = self._summarize_pr_key(repo, issue_search_result)
        if pr_key not in self._state:
            self._state[pr_key] = current_state
            logger.debug("Initialized state for %s", pr_key)
            return pr_key

        previous = self._state[pr_key]

        pr_ref = PrRef(
            repo=repo,
            number=number,
        )
        self._handle_mergeable_change(
            pr_ref,
            previous,
            mergeable_state,
        )
        self._handle_new_reviews(
            pr_ref,
            reviews,
            review_comments,
            previous,
        )
        self._handle_new_issue_comments(
            pr_ref,
            issue_comments,
            previous.last_issue_comment_id,
        )
        self._handle_ci_status_change(pr_ref, previous, check_runs)

        self._state[pr_key] = current_state
        return pr_key

    @staticmethod
    def _summarize_pr_key(repo: str, pr_summary: SearchIssueItem) -> str:
        return f"{repo}#{pr_summary.number} ({pr_summary.title})"

    def _handle_mergeable_change(
        self,
        pr: PrRef,
        previous: PrState,
        current: str | None,
    ) -> None:
        if current == previous.mergeable_state:
            return
        if current == PullRequestMergeableState.DIRTY:
            self._hooks.on_merge_conflict(pr)

    def _handle_new_reviews(
        self,
        pr: PrRef,
        reviews: list[Review],
        review_comments: list[ReviewComment],
        previous: PrState,
    ) -> None:
        new_reviews = [
            review for review in reviews if review.id > previous.last_review_id
        ]
        new_review_comments = [
            comment
            for comment in review_comments
            if comment.id > previous.last_review_comment_id
        ]
        if not new_reviews and not new_review_comments:
            return

        review_by_id = {review.id: review for review in reviews}
        review_ids = {review.id for review in new_reviews} | {
            comment.review_id for comment in new_review_comments
        }

        grouped_comments: dict[int, list[ReviewComment]] = {}
        for comment in sorted(new_review_comments, key=lambda item: item.id):
            grouped_comments.setdefault(comment.review_id, []).append(comment)

        review_entries: list[ReviewWithComments] = []
        for review_id in sorted(review_ids):
            review = review_by_id.get(review_id)
            if not review:
                continue
            review_entries.append(
                ReviewWithComments(
                    review=review,
                    comments=grouped_comments.get(review_id, []),
                )
            )

        if not review_entries:
            return
        self._hooks.on_new_review(pr, review_entries)

    def _handle_new_issue_comments(
        self,
        pr: PrRef,
        comments: list[IssueComment],
        last_comment_id: int,
    ) -> None:
        new_comments = [comment for comment in comments if comment.id > last_comment_id]
        if not new_comments:
            return
        self._hooks.on_new_issue_comment(
            pr,
            sorted(new_comments, key=lambda item: item.id),
        )

    def _handle_ci_status_change(
        self,
        pr: PrRef,
        previous: PrState,
        current: list[CheckRun],
    ) -> None:
        new_runs = [
            run
            for run in current
            if run.id is not None and run.id > previous.last_check_run_id
        ]
        if not new_runs:
            return
        for check_run in sorted(new_runs, key=lambda item: item.id or 0):
            self._hooks.on_ci_status_change(pr, check_run)

    @staticmethod
    def _max_id(items: Iterable[HasId]) -> int:
        return max((item.id for item in items), default=0)


class PrMonitorRunner:
    def __init__(
        self,
        username: str,
        token: str,
        hooks: PrMonitorHooks,
    ) -> None:
        self._username = username
        self._token = token
        self._hooks = hooks

    async def run(self) -> None:
        async with build_async_github_client(self._token) as client:
            monitor = PrMonitor(
                client,
                self._username,
                self._hooks,
            )
            while True:
                logger.debug("Starting polling cycle")
                await monitor.poll_once()
