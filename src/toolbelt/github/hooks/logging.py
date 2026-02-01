from toolbelt.github.api import IssueComment
from toolbelt.github.hooks.base import (
    AbstractPrMonitorHooks,
    PrRef,
    ReviewWithComments,
)
from toolbelt.logger import logger


class LoggingPrMonitorHooks(AbstractPrMonitorHooks):
    async def on_merge_conflict(self, pr: PrRef) -> None:
        logger.info(
            "Merge conflict detected for %s (%s)",
            pr.repo,
            pr.number,
        )

    async def on_new_review(
        self,
        pr: PrRef,
        reviews: list[ReviewWithComments],
    ) -> None:
        logger.info(
            "New reviews for %s#%s (%s reviews, %s comments)",
            pr.repo,
            pr.number,
            len(reviews),
            sum(len(entry.comments) for entry in reviews),
        )

    def on_new_issue_comment(
        self,
        pr: PrRef,
        comments: list[IssueComment],
    ) -> None:
        logger.info(
            "New comments for %s#%s (%s)",
            pr.repo,
            pr.number,
            len(comments),
        )

    async def on_ci_status_change(
        self,
        pr: PrRef,
        has_failure: bool,
    ) -> None:
        logger.info(
            "CI checks completed for %s#%s (has_failure=%s)",
            pr.repo,
            pr.number,
            has_failure,
        )
