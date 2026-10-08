"""Whether a PR has been posted in the team's code-review Slack channel.

The channel and the token to search it are read from env vars rather than
named here, since this is a public repo: ``SLACK_PUBLISHED_CHANNEL`` (bare
name, no leading ``#``) and ``SLACK_USER_TOKEN``. Slack's search API only
works with a user token (``xoxp-...``) — it isn't exposed to bot tokens.
"""

import os

import httpx

_SEARCH_URL = "https://slack.com/api/search.messages"


def _has_results(payload: dict) -> bool:
    """True if a `search.messages` payload found at least one match."""
    return payload.get("messages", {}).get("total", 0) > 0


async def is_published(pr_url: str) -> bool | None:
    """True/False if `pr_url` has been posted in the configured channel.

    None if the check isn't configured (no token/channel set) or the Slack
    call itself failed — this is a best-effort display check, not something
    worth failing `tree` over, so callers should treat None as "unknown" and
    just omit the field rather than show it as "not published".
    """
    token = os.getenv("SLACK_USER_TOKEN")
    channel = os.getenv("SLACK_PUBLISHED_CHANNEL")
    if not token or not channel:
        return None

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(
                _SEARCH_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={"query": f'"{pr_url}" in:#{channel}'},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError:
            return None

    if not payload.get("ok"):
        return None
    return _has_results(payload)
