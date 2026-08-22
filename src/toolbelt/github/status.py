import asyncio

from toolbelt.github.api import (
    CIStatus,
    PullRequestState,
    ReviewDecision,
    get_pr_status,
    get_review_requests,
)
from toolbelt.logger import logger


async def display_status(username: str, token: str) -> None:
    logger.info("🔍 Fetching your PRs and review requests...")

    my_prs, review_requests = await asyncio.gather(
        get_pr_status(username, token),
        get_review_requests(username, token),
    )

    if my_prs:
        logger.info(f"\n📝 Your PRs ({len(my_prs)}):")
        logger.info("=" * 80)

        for pr in my_prs:
            status_icons = []
            if pr.draft:
                status_icons.append("📝 draft")
            if pr.state == PullRequestState.CLOSED:
                status_icons.append("✅ closed")
            elif pr.state == PullRequestState.OPEN:
                if pr.review_decision == ReviewDecision.APPROVED:
                    status_icons.append("✅ approved")
                elif pr.review_decision == ReviewDecision.CHANGES_REQUESTED:
                    status_icons.append("🔄 changes requested")
                elif pr.review_decision == ReviewDecision.COMMENTED:
                    status_icons.append("💬 commented")
                else:
                    status_icons.append("⏳ pending review")

            if pr.ci_status:
                ci_icon = {
                    CIStatus.SUCCESS: "✅",
                    CIStatus.FAILURE: "❌",
                    CIStatus.IN_PROGRESS: "🔄",
                    CIStatus.PENDING: "⏳",
                }.get(pr.ci_status, "❓")
                status_icons.append(f"{ci_icon} CI: {pr.ci_status.value}")

            status_text = " | ".join(status_icons) if status_icons else "No status"

            logger.info(f"#{pr.number} {pr.title}")
            logger.info(f"  📁 {pr.repo}")
            logger.info(f"  🔗 {pr.url}")
            logger.info(f"  📊 {status_text}")
            logger.info(f"  ⏰ Created {pr.time_open} ago")
            logger.info("")
    else:
        logger.info("\n📝 No PRs found")

    if review_requests:
        logger.info(f"\n👀 Review Requests ({len(review_requests)}):")
        logger.info("=" * 80)

        for pr in review_requests:
            status_icons = []
            if pr.draft:
                status_icons.append("📝 draft")

            if pr.ci_status:
                ci_icon = {
                    CIStatus.SUCCESS: "✅",
                    CIStatus.FAILURE: "❌",
                    CIStatus.IN_PROGRESS: "🔄",
                    CIStatus.PENDING: "⏳",
                }.get(pr.ci_status, "❓")
                status_icons.append(f"{ci_icon} CI: {pr.ci_status.value}")

            status_text = " | ".join(status_icons) if status_icons else "No status"

            logger.info(f"#{pr.number} {pr.title}")
            logger.info(f"  📁 {pr.repo}")
            logger.info(f"  🔗 {pr.url}")
            logger.info(f"  📊 {status_text}")
            logger.info(f"  ⏰ Created {pr.time_open} ago")
            logger.info("")
    else:
        logger.info("\n👀 No review requests found")
