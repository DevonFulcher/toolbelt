import httpx

from toolbelt.github.api import IssueComment
from toolbelt.github.client import GithubClient
from toolbelt.github.hooks.base import AbstractPrMonitorHooks, PrRef, ReviewWithComments
from toolbelt.logger import logger


class CursorPrMonitorHooks(AbstractPrMonitorHooks):
    def __init__(self, client: GithubClient) -> None:
        self._client = client

    async def on_merge_conflict(self, pr: PrRef) -> None:
        if not pr.repo.startswith("dbt-labs/"):
            logger.info("Skipping merge conflict hook for %s", pr.pr_url)
            return

        url = f"https://api.github.com/repos/{pr.repo}/issues/{pr.number}/comments"
        try:
            logger.info("Posting merge conflict comment for %s", pr.pr_url)
            response = await self._client.post(
                url, json={"body": "@cursor address the merge conflicts"}
            )
            response.raise_for_status()
            logger.info("Posted merge conflict comment for %s", pr.pr_url)
        except httpx.HTTPError:
            logger.exception("Failed to comment on merge conflict for %s", pr.pr_url)

    async def on_new_review(
        self,
        pr: PrRef,
        reviews: list[ReviewWithComments],
    ) -> None:
        if not pr.repo.startswith("dbt-labs/"):
            logger.info("Skipping review hook for %s", pr.pr_url)
            return

        bot_logins = {
            "cursor[bot]",
            "copilot-pull-request-reviewer[bot]",
        }
        body = (
            "@cursor determine if this needs to be fixed and update the code if so. "
            "Avoid scope creep"
        )

        for entry in reviews:
            # Only reply to thread-starting comments from bots.
            if not entry.comments:
                continue
            thread_starters = [
                comment
                for comment in entry.comments
                if comment.in_reply_to_id is None and comment.user.login in bot_logins
            ]
            if not thread_starters:
                continue
            for thread_start in sorted(thread_starters, key=lambda c: c.id):
                if any(
                    comment.in_reply_to_id == thread_start.id
                    and comment.user.login in bot_logins
                    for comment in entry.comments
                ):
                    continue
                url = (
                    f"https://api.github.com/repos/{pr.repo}/pulls/{pr.number}"
                    f"/comments/{thread_start.id}/replies"
                )
                try:
                    logger.info(
                        "Replying to review comment %s for %s",
                        thread_start.id,
                        pr.pr_url,
                    )
                    response = await self._client.post(url, json={"body": body})
                    response.raise_for_status()
                    logger.info(
                        "Replied to review comment %s for %s",
                        thread_start.id,
                        pr.pr_url,
                    )
                except httpx.HTTPError:
                    logger.exception(
                        "Failed to reply to review comment %s for %s",
                        thread_start.id,
                        pr.pr_url,
                    )

    def on_new_issue_comment(
        self,
        pr: PrRef,
        comments: list[IssueComment],
    ) -> None:
        pass

    async def on_ci_status_change(
        self,
        pr: PrRef,
        has_failure: bool,
    ) -> None:
        if not pr.repo.startswith("dbt-labs/"):
            logger.info("Skipping CI hook for %s", pr.pr_url)
            return

        if not has_failure:
            logger.info("CI passed for %s; no action", pr.pr_url)
            return

        url = f"https://api.github.com/repos/{pr.repo}/issues/{pr.number}/comments"
        try:
            logger.info("Posting CI failure comment for %s", pr.pr_url)
            response = await self._client.post(
                url, json={"body": "@cursor investigate and fix the CI failure"}
            )
            response.raise_for_status()
            logger.info("Posted CI failure comment for %s", pr.pr_url)
        except httpx.HTTPError:
            logger.exception("Failed to comment on CI failure for %s", pr.pr_url)
