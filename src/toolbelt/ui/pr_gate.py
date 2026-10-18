"""Tells the status poll when GitHub has nothing new for a PR, for free.

GraphQL has no conditional requests, but REST does: a request that sends the
``ETag`` of the last answer back as ``If-None-Match`` gets ``304 Not
Modified``, and GitHub does not count a 304 against the rate limit. ``PrGate``
keeps the ETag of three REST resources that together move whenever the parts
of a PR the UI shows move:

* the pull request (state, draft, mergeability, assignees, requested
  reviewers, and ``updated_at``, which any comment or review bumps);
* the check runs of its head commit;
* the commit statuses of its head commit.

While all three answer 304 the poll reuses the previous lookup instead of
running the GraphQL query (see ``StatusRefresher``). The gate does not see
everything (a review thread being resolved, or the base branch moving under the
PR), so the refresher still runs the full lookup every ``full_interval``.

The request is a seam (``ConditionalGet``) so tests inject canned answers.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from toolbelt import logged_process
from toolbelt.ui.live_status import Fetched, TrackedBranch


@dataclass(frozen=True)
class ConditionalAnswer:
    """GitHub's answer to a conditional GET: ``changed`` is ``False`` for a
    304; ``etag`` is the resource's current ETag."""

    changed: bool
    etag: str | None


#: (REST path, the ETag last seen or ``None``) -> GitHub's answer.
ConditionalGet = Callable[[str, str | None], Awaitable[ConditionalAnswer]]


async def gh_conditional_get(path: str, etag: str | None) -> ConditionalAnswer:
    command = ["gh", "api", "--include", path]
    if etag:
        command += ["-H", f"If-None-Match: {etag}"]
    process = await logged_process.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    head = stdout.decode().split("\r\n\r\n", 1)[0].split("\n\n", 1)[0]
    status_line, *headers = head.splitlines() or [""]
    # `gh api` exits 1 for a 304 as it does for an error, but still prints the
    # response head.
    if process.returncode != 0 and status_line.split()[1:2] != ["304"]:
        raise RuntimeError(f"gh api {path} failed: {stderr.decode().strip()}")
    new_etag = next(
        (
            line.split(":", 1)[1].strip()
            for line in headers
            if line.lower().startswith("etag:")
        ),
        None,
    )
    return ConditionalAnswer(changed=status_line.split()[1] != "304", etag=new_etag)


class PrGate:
    """Remembers the ETags of the resources behind each PR."""

    def __init__(self, get: ConditionalGet = gh_conditional_get) -> None:
        self._get = get
        self._etags: dict[str, str] = {}

    @staticmethod
    def _paths(slug: str, number: int, head_oid: str) -> list[str]:
        return [
            f"repos/{slug}/pulls/{number}",
            f"repos/{slug}/commits/{head_oid}/check-runs",
            f"repos/{slug}/commits/{head_oid}/status",
        ]

    async def unchanged(self, slug: str, number: int, head_oid: str) -> bool:
        """Whether none of the PR's resources changed since this gate last
        saw them. A resource it has not seen yet counts as changed. The ETags
        it learns are kept either way, so the next call compares with now."""
        answers = await asyncio.gather(
            *(
                self._ask(path, self._etags.get(path))
                for path in self._paths(slug, number, head_oid)
            )
        )
        return not any(answers)

    async def remember(self, slug: str, number: int, head_oid: str) -> None:
        """Learns the ETag of each resource not seen yet (right after a full
        lookup, so the next poll has something to compare with)."""
        await asyncio.gather(
            *(
                self._ask(path, None)
                for path in self._paths(slug, number, head_oid)
                if path not in self._etags
            )
        )

    async def _ask(self, path: str, etag: str | None) -> bool:
        """Whether ``path`` changed; its new ETag is stored."""
        answer = await self._get(path, etag)
        if answer.etag:
            self._etags[path] = answer.etag
        return answer.changed


class GithubChangeGate:
    """Adapts ``PrGate`` to what ``StatusRefresher`` asks: a branch's repo
    resolved to ``owner/name``, and the PR number and head commit read from
    its last lookup."""

    def __init__(
        self, gate: PrGate, slug_of: Callable[[int], Awaitable[str | None]]
    ) -> None:
        self._gate = gate
        self._slug_of = slug_of

    async def unchanged(self, branch: TrackedBranch, previous: Fetched) -> bool:
        facts = previous.facts
        slug = await self._slug_of(branch.repo_id)
        if facts is None or slug is None:
            return False
        return await self._gate.unchanged(slug, facts.number, facts.head_oid)

    async def remember(self, branch: TrackedBranch, fetched: Fetched) -> None:
        facts = fetched.facts
        slug = await self._slug_of(branch.repo_id)
        if facts is not None and slug is not None:
            await self._gate.remember(slug, facts.number, facts.head_oid)
