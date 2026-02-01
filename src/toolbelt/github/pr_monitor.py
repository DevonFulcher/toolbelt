import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

import httpx

from toolbelt.github.api import (
    CheckRun,
    CheckRunConclusion,
    CheckRunsResponse,
    CheckRunStatus,
    IssueComment,
    PullRequestDetails,
    PullRequestMergeableState,
    Review,
    ReviewComment,
    SearchIssueItem,
    search_open_authored_prs,
)
from toolbelt.github.client import GithubClient, build_async_github_client
from toolbelt.github.hooks.base import (
    AbstractPrMonitorHooks,
    PrRef,
    ReviewWithComments,
)
from toolbelt.logger import logger

HooksFactory = Callable[[GithubClient], AbstractPrMonitorHooks]


@dataclass
class PrState:
    mergeable_state: str | None = None
    last_check_run_id: int = 0
    last_review_id: int = 0
    last_issue_comment_id: int = 0
    last_review_comment_id: int = 0
    ci_all_completed: bool = False


class HasId(Protocol):
    id: int


class PrMonitor:
    def __init__(
        self,
        client: GithubClient,
        username: str,
        hooks: AbstractPrMonitorHooks,
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
        logger.info("Found %s open PRs", len(prs))

        seen_keys: set[str] = set()
        for pr in prs:
            pr_key = await self._process_pr(pr)
            if pr_key:
                seen_keys.add(pr_key)

        stale_keys = set(self._state.keys()) - seen_keys
        for key in stale_keys:
            logger.info("Removing stale PR state for %s", key)
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
        ci_all_completed = (
            all(run.status == CheckRunStatus.COMPLETED for run in check_runs)
            if check_runs
            else False
        )

        current_state = PrState(
            mergeable_state=mergeable_state,
            last_check_run_id=self._max_id(check_runs),
            last_review_id=self._max_id(reviews),
            last_issue_comment_id=self._max_id(issue_comments),
            last_review_comment_id=self._max_id(review_comments),
            ci_all_completed=ci_all_completed,
        )

        pr_key = self._summarize_pr_key(repo, issue_search_result)
        if pr_key not in self._state:
            self._state[pr_key] = current_state
            return pr_key

        previous = self._state[pr_key]

        pr_ref = PrRef(
            repo=repo,
            number=number,
        )
        await self._handle_mergeable_change(
            pr_ref,
            previous,
            mergeable_state,
        )
        await self._handle_new_reviews(
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
        await self._handle_ci_status_change(pr_ref, previous, check_runs)

        self._state[pr_key] = current_state
        return pr_key

    @staticmethod
    def _summarize_pr_key(repo: str, pr_summary: SearchIssueItem) -> str:
        return f"{repo}#{pr_summary.number}"

    async def _handle_mergeable_change(
        self,
        pr: PrRef,
        previous: PrState,
        current: str | None,
    ) -> None:
        if current == previous.mergeable_state:
            return
        if current == PullRequestMergeableState.DIRTY:
            await self._hooks.on_merge_conflict(pr)

    async def _handle_new_reviews(
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
        if not new_reviews:
            return

        review_by_id = {review.id: review for review in reviews}
        review_ids = {review.id for review in new_reviews}

        grouped_comments: dict[int, list[ReviewComment]] = {}
        for comment in sorted(new_review_comments, key=lambda item: item.id):
            if comment.pull_request_review_id is None:
                continue
            if comment.pull_request_review_id not in review_ids:
                continue
            grouped_comments.setdefault(comment.pull_request_review_id, []).append(
                comment
            )

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
        await self._hooks.on_new_review(pr, review_entries)

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

    async def _handle_ci_status_change(
        self,
        pr: PrRef,
        previous: PrState,
        current: list[CheckRun],
    ) -> None:
        if not current:
            return

        all_completed = all(run.status == CheckRunStatus.COMPLETED for run in current)

        # Only fire once when transitioning to "all completed"
        if all_completed and not previous.ci_all_completed:
            has_failure = any(
                run.conclusion == CheckRunConclusion.FAILURE for run in current
            )
            await self._hooks.on_ci_status_change(pr, has_failure)

    @staticmethod
    def _max_id(items: Iterable[HasId]) -> int:
        return max((item.id for item in items), default=0)


class PrMonitorRunner:
    def __init__(
        self,
        username: str,
        token: str,
        hooks: AbstractPrMonitorHooks,
    ) -> None:
        self._username = username
        self._token = token
        self._hooks = hooks
        self._poll_interval = 30.0

    async def run(self) -> None:
        async with build_async_github_client(self._token) as client:
            monitor = PrMonitor(
                client,
                self._username,
                self._hooks,
            )
            while True:
                try:
                    await monitor.poll_once()
                except httpx.HTTPError:
                    logger.exception("Polling cycle failed; retrying after delay")
                logger.info("Sleeping for %s seconds", self._poll_interval)
                await asyncio.sleep(self._poll_interval)
