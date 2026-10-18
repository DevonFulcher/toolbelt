"""Which tracked branches a GitHub webhook says changed.

``gh webhook forward`` (see ``webhook_forwarders.py``) replays GitHub's
webhooks as POSTs to ``/api/github-webhook``. A webhook is only a hint: the
payload is never used as data, it names the PRs to look up again, and the
refresher fetches the truth from GitHub. So a spurious or forged POST costs a
lookup and nothing else.
"""

from dataclasses import dataclass

#: The events that move something a card shows (PR state, reviews, comments,
#: CI, pushes). Passed to ``gh webhook forward --events``.
FORWARDED_EVENTS = (
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_review_thread",
    "issue_comment",
    "check_run",
    "check_suite",
    "status",
    "push",
)


@dataclass(frozen=True)
class Affected:
    """Branches and PR numbers of one repo (lower-case ``owner/name``)."""

    repo: str
    branches: frozenset[str]
    numbers: frozenset[int]


def _pull_requests(nodes: list[dict]) -> tuple[set[str], set[int]]:
    return (
        {node["head"]["ref"] for node in nodes if node.get("head")},
        {node["number"] for node in nodes if "number" in node},
    )


def affected(event: str, payload: dict) -> Affected | None:
    """What ``event`` (the ``X-GitHub-Event`` header) with ``payload``
    touched, or ``None`` for an event that touches no branch (``ping``)."""
    repository = payload.get("repository")
    if not repository:
        return None
    branches: set[str] = set()
    numbers: set[int] = set()
    if event in (
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_review_thread",
    ):
        branches, numbers = _pull_requests([payload["pull_request"]])
    elif event in ("check_run", "check_suite"):
        run = payload[event]
        branches, numbers = _pull_requests(run.get("pull_requests") or [])
        head_branch = run.get("head_branch") or (run.get("check_suite") or {}).get(
            "head_branch"
        )
        if head_branch:
            branches.add(head_branch)
    elif event == "status":
        branches = {branch["name"] for branch in payload.get("branches") or []}
    elif event == "issue_comment":
        if "pull_request" in payload["issue"]:
            numbers = {payload["issue"]["number"]}
    elif event == "push":
        ref = payload["ref"]
        if ref.startswith("refs/heads/"):
            branches = {ref.removeprefix("refs/heads/")}
    if not branches and not numbers:
        return None
    return Affected(
        repo=repository["full_name"].lower(),
        branches=frozenset(branches),
        numbers=frozenset(numbers),
    )
