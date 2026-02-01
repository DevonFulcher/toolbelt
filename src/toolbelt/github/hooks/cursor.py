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
            return

        url = f"https://api.github.com/repos/{pr.repo}/issues/{pr.number}/comments"
        try:
            response = await self._client.post(
                url, json={"body": "@cursor address the merge conflicts"}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Failed to comment on merge conflict for %s", pr.pr_url)

    async def on_new_review(
        self,
        pr: PrRef,
        reviews: list[ReviewWithComments],
    ) -> None:
        if not pr.repo.startswith("dbt-labs/"):
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
            if entry.review.user.login not in bot_logins:
                continue
            # Only reply to the first comment of each bot review
            if not entry.comments:
                continue
            first_comment = min(entry.comments, key=lambda c: c.id)
            url = (
                f"https://api.github.com/repos/{pr.repo}/pulls/{pr.number}"
                f"/comments/{first_comment.id}/replies"
            )
            try:
                response = await self._client.post(url, json={"body": body})
                response.raise_for_status()
            except httpx.HTTPError:
                logger.exception(
                    "Failed to reply to review comment %s for %s",
                    first_comment.id,
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
            return

        if not has_failure:
            return

        url = f"https://api.github.com/repos/{pr.repo}/issues/{pr.number}/comments"
        try:
            response = await self._client.post(
                url, json={"body": "@cursor investigate and fix the CI failure"}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Failed to comment on CI failure for %s", pr.pr_url)
