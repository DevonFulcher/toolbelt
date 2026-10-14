"""The user's open PRs that are not tracked stack branches, for the UI's
"Open PRs not tracked by toolbelt" callout: links only (repo, number, title,
age), never statuses.

One GraphQL search per refresh (``gh api graphql``) lists every open PR the
user authored in non-archived repos, with its head branch. A PR is left out
when its repo and head branch are a tracked branch in the stack DB, so what
remains is work toolbelt does not know about (a branch pushed without it, a
fork's PR, a repo that is not registered). Refreshing is slow (every few
minutes, ``UntrackedPrFeed``) and, like the status polling, only while a
browser is watching.

The ``gh`` call and the lookup of a checkout's remote are injected, so tests
drive everything with canned payloads.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from toolbelt import logged_process
from toolbelt.ui import api_models as api
from toolbelt.ui.live_status import DEFAULT_TICK_SECONDS, EventHub

DEFAULT_INTERVAL_SECONDS = 180.0

# Open PRs authored by the authenticated user, in repos that are not
# archived. 100 is GitHub's page size; `issueCount` says when there are more.
SEARCH_QUERY = "is:pr is:open author:@me archived:false sort:created-desc"

PR_QUERY = """
query($search: String!) {
  search(query: $search, type: ISSUE, first: 100) {
    issueCount
    nodes {
      ... on PullRequest {
        number title url createdAt isDraft headRefName
        repository { nameWithOwner isArchived }
      }
    }
  }
}
"""

#: () -> the GraphQL response (``{"data": ...}``).
SearchRunner = Callable[[], Awaitable[dict]]
#: A checkout's ``origin`` URL (``None``: no checkout or no remote).
RemoteLookup = Callable[[Path], Awaitable[str | None]]


async def run_search_graphql(query: str, search: str, **variables: str) -> dict:
    """``gh api graphql`` for a search query, with its ``$search`` (and any
    other string) variables. A search across organizations answers what it may
    and reports the rest as errors (a SAML-protected organization the token is
    not authorized for), which makes `gh` exit non-zero with partial data:
    that is still a result. No data at all is a failure."""
    fields: list[str] = []
    for name, value in {"search": search, **variables}.items():
        fields += ["-f", f"{name}={value}"]
    process = await logged_process.create_subprocess_exec(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={query}",
        *fields,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        try:
            body = json.loads(stdout)
        except json.JSONDecodeError:
            body = {}
        if not (body.get("data") or {}).get("search"):
            raise RuntimeError(f"gh api graphql failed: {stderr.decode().strip()}")
        return body
    return json.loads(stdout)


async def gh_search() -> dict:
    return await run_search_graphql(PR_QUERY, SEARCH_QUERY)


async def origin_remote(path: Path) -> str | None:
    if not path.is_dir():
        return None
    result = await asyncio.to_thread(
        logged_process.run,
        ["git", "remote", "get-url", "origin"],
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


_SLUG_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


def repo_slug(remote_url: str | None) -> str | None:
    """``owner/name`` of a GitHub-shaped remote URL (https or ssh), lower
    case (GitHub names are case-insensitive)."""
    if not remote_url:
        return None
    match = _SLUG_RE.search(remote_url.strip())
    return f"{match.group(1)}/{match.group(2)}".lower() if match else None


@dataclass(frozen=True)
class OpenPr:
    repo: str
    number: int
    title: str
    url: str
    created_at: datetime
    is_draft: bool
    head_branch: str
    archived: bool


@dataclass(frozen=True)
class Search:
    prs: list[OpenPr]
    #: How many open PRs GitHub counted (more than ``prs`` when truncated).
    total: int
    #: PRs GitHub would not show (an organization the token is not
    #: authorized for): counted from the response's errors.
    hidden: int = 0


def parse_search(payload: dict) -> Search:
    """A ``PR_QUERY`` response. Nodes that are not pull requests (the search
    spans issues too) come back as empty objects and are skipped."""
    search = payload["data"]["search"]
    prs = [
        OpenPr(
            repo=node["repository"]["nameWithOwner"],
            number=node["number"],
            title=node["title"],
            url=node["url"],
            created_at=datetime.fromisoformat(node["createdAt"]),
            is_draft=node["isDraft"],
            head_branch=node["headRefName"],
            archived=node["repository"]["isArchived"],
        )
        for node in search["nodes"]
        if node
    ]
    return Search(
        prs=prs, total=search["issueCount"], hidden=len(payload.get("errors") or [])
    )


def untracked(prs: list[OpenPr], tracked: set[tuple[str, str]]) -> list[OpenPr]:
    """``prs`` that toolbelt does not track: those in archived repos are left
    out, and so is any PR whose ``(repo, head branch)`` is in ``tracked``
    (repos as lower-case ``owner/name``). Ordered by repo, then newest PR
    number first."""
    kept = [
        pr
        for pr in prs
        if not pr.archived and (pr.repo.lower(), pr.head_branch) not in tracked
    ]
    return sorted(kept, key=lambda pr: (pr.repo.lower(), -pr.number))


@dataclass(frozen=True)
class TrackedRepo:
    """A registered repo: where its checkout is, what its origin was when it
    was registered, and the branches tracked in it."""

    path: Path
    remote_url: str | None
    branches: frozenset[str]


async def tracked_pairs(
    repos: list[TrackedRepo], remote_of: RemoteLookup
) -> set[tuple[str, str]]:
    """Every tracked branch as ``(owner/name, branch)``; a repo whose remote
    cannot be resolved cannot be matched (its PRs show as untracked)."""
    pairs: set[tuple[str, str]] = set()
    for repo in repos:
        slug = repo_slug(repo.remote_url) or repo_slug(await remote_of(repo.path))
        if slug:
            pairs.update((slug, branch) for branch in repo.branches)
    return pairs


class UntrackedPrFeed:
    """Keeps the list current for the connected pages: refreshed at most
    every ``interval`` seconds, only while a browser is watching, publishing
    an ``untracked`` event when the list changed."""

    def __init__(
        self,
        *,
        repos: Callable[[], list[TrackedRepo]],
        search: SearchRunner,
        hub: EventHub,
        remote_of: RemoteLookup = origin_remote,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        tick: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._repos = repos
        self._search = search
        self._hub = hub
        self._remote_of = remote_of
        self._clock = clock
        self._sleep = sleep
        self._interval = interval
        self._tick = tick
        self._last_at: float | None = None
        self._current: api.UntrackedPrs = api.UntrackedLoading(state="loading")

    def get(self) -> api.UntrackedPrs:
        return self._current

    def is_due(self) -> bool:
        return self._last_at is None or self._clock() - self._last_at >= self._interval

    async def _fetch(self) -> api.UntrackedPrs:
        try:
            found = parse_search(await self._search())
            pairs = await tracked_pairs(self._repos(), self._remote_of)
        except Exception as error:  # noqa: BLE001
            # A background refresh with nobody to propagate to: say what went
            # wrong in the callout, retry after the interval.
            return api.UntrackedFailed(state="failed", error=str(error))
        return api.UntrackedLoaded(
            state="loaded",
            truncated=found.total > len(found.prs) + found.hidden,
            hidden=found.hidden,
            prs=[
                api.UntrackedPr(
                    repo=pr.repo,
                    number=pr.number,
                    title=pr.title,
                    url=pr.url,
                    created_at=pr.created_at.isoformat().replace("+00:00", "Z"),
                    is_draft=pr.is_draft,
                )
                for pr in untracked(found.prs, pairs)
            ],
        )

    async def run_once(self) -> bool:
        """Refresh now. Returns whether the list changed (and was published)."""
        current = await self._fetch()
        self._last_at = self._clock()
        if current == self._current:
            return False
        self._current = current
        self._hub.publish("untracked", {"untracked": current.model_dump(mode="json")})
        return True

    async def run(self) -> None:
        while True:
            await self._hub.wait_for_subscriber()
            while self._hub.watched():
                if self.is_due():
                    await self.run_once()
                await self._sleep(self._tick)
