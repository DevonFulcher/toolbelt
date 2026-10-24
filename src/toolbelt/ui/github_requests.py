"""Every GitHub request the UI server makes goes through ``GithubRequests``.

The server asks GitHub from several loops (the PR status lookups, the ETag
gate, the untracked-PR search, the aggregate history, the admin check). They
spend one shared budget, so they have to stop together when it runs out, and
something has to know who spends it. This class owns that:

* GraphQL and REST have separate budgets (``graphql`` and ``core``). The
  remaining count and reset time of each come from the response headers. When
  a budget is exhausted (GitHub says so, or ``remaining`` hits 0) every call
  that needs it raises ``RateLimited`` at once, without sending anything, until
  it resets. Without a reset time the wait starts at ``BACKOFF_SECONDS`` and
  doubles up to ``MAX_BACKOFF_SECONDS``.
* ``usage()`` counts requests per caller (``status``, ``gate``, ``untracked``,
  ``aggregate``, ``admin``), so what is spending the budget can be read off.
* Requests share one connection pool, authenticated with the token of the
  signed-in ``gh`` (read once).

The transport is a seam (``httpx.MockTransport`` in tests).
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from toolbelt import logged_process
from toolbelt.logger import logger

#: ``TT_GITHUB_API_URL`` points the server at another GitHub API (the demo's
#: fake, ``tools/ui-demo/github_server.py``).
API_URL = os.environ.get("TT_GITHUB_API_URL", "https://api.github.com")
BACKOFF_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 600.0
_REQUEST_TIMEOUT_SECONDS = 30.0

GRAPHQL = "graphql"
CORE = "core"


class GithubUnavailable(RuntimeError):
    """GitHub answered with a server error (a 5xx): for a heavy query, usually
    that it took too long."""


class RateLimited(RuntimeError):
    """A GitHub budget is used up; ``resource`` is ``graphql`` or ``core``
    and ``wait`` the seconds until it may be used again."""

    def __init__(self, resource: str, wait: float) -> None:
        super().__init__(f"GitHub {resource} rate limit exceeded")
        self.resource = resource
        self.wait = wait


@dataclass(frozen=True)
class ConditionalAnswer:
    """GitHub's answer to a conditional GET: ``changed`` is ``False`` for a
    304; ``etag`` is the resource's current ETag."""

    changed: bool
    etag: str | None


@dataclass
class _Budget:
    limit: int | None = None
    remaining: int | None = None
    #: When it resets (wall clock), as GitHub last said.
    reset: datetime | None = None
    #: ``clock`` time before which nothing is sent.
    blocked_until: float = 0.0
    backoff: float = 0.0


@dataclass
class _Calls:
    requests: int = 0
    not_modified: int = 0
    rate_limited: int = 0
    failed: int = 0


@dataclass
class _State:
    budgets: dict[str, _Budget] = field(default_factory=dict)
    calls: dict[str, _Calls] = field(default_factory=dict)


async def gh_auth_token() -> str:
    process = await logged_process.create_subprocess_exec(
        "gh",
        "auth",
        "token",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"gh auth token failed: {stderr.decode().strip()}")
    return stdout.decode().strip()


class GithubRequests:
    def __init__(
        self,
        *,
        token: Callable[[], Awaitable[str]] = gh_auth_token,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._token = token
        self._transport = transport
        self._clock = clock
        self._wall_clock = wall_clock
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._state = _State()

    # ---- the requests -------------------------------------------------------

    async def graphql(
        self,
        query: str,
        variables: Mapping[str, str | int],
        *,
        caller: str,
        partial: str | None = None,
    ) -> dict:
        """The GraphQL response (``{"data": ...}``). A response with errors
        raises, unless ``partial`` names a field of ``data`` that holds a result
        (a search across organizations answers what it may and reports the
        rest, for instance a SAML-protected one, as errors)."""
        response = await self._send(
            "POST",
            f"{API_URL}/graphql",
            GRAPHQL,
            caller,
            json={"query": query, "variables": variables},
        )
        body = response.json()
        errors = body.get("errors")
        if errors and any(_is_rate_limit(error) for error in errors):
            raise self._exhausted(GRAPHQL, response, caller)
        if errors and not (partial and (body.get("data") or {}).get(partial)):
            self._calls(caller).failed += 1
            raise RuntimeError(f"GitHub GraphQL failed: {errors}")
        return body

    async def conditional_get(
        self, path: str, etag: str | None, *, caller: str
    ) -> ConditionalAnswer:
        """GET a REST ``path`` with ``If-None-Match``; a 304 is not counted
        against the rate limit."""
        headers = {"If-None-Match": etag} if etag else {}
        response = await self._send(
            "GET", f"{API_URL}/{path}", CORE, caller, headers=headers
        )
        return ConditionalAnswer(
            changed=response.status_code != 304, etag=response.headers.get("etag")
        )

    async def get_json(self, path: str, *, caller: str) -> Any:
        """The JSON of a REST GET: an object, or a list for a collection."""
        response = await self._send("GET", f"{API_URL}/{path}", CORE, caller)
        return response.json()

    async def delete(self, path: str, *, caller: str) -> None:
        await self._send("DELETE", f"{API_URL}/{path}", CORE, caller)

    # ---- what it knows ------------------------------------------------------

    def usage(self) -> dict:
        """Each budget as GitHub last reported it and the requests each caller
        has made since the server started."""
        now = self._clock()
        return {
            "budgets": {
                name: {
                    "limit": budget.limit,
                    "remaining": budget.remaining,
                    "reset": budget.reset.isoformat() if budget.reset else None,
                    "blocked_for": max(budget.blocked_until - now, 0.0),
                }
                for name, budget in self._state.budgets.items()
            },
            "callers": {
                name: {
                    "requests": calls.requests,
                    "not_modified": calls.not_modified,
                    "rate_limited": calls.rate_limited,
                    "failed": calls.failed,
                }
                for name, calls in self._state.calls.items()
            },
        }

    def blocked(self, resource: str) -> float:
        """Seconds until ``resource`` may be used again (0 when it may)."""
        budget = self._state.budgets.get(resource)
        return max(budget.blocked_until - self._clock(), 0.0) if budget else 0.0

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ---- internals ----------------------------------------------------------

    def _calls(self, caller: str) -> _Calls:
        return self._state.calls.setdefault(caller, _Calls())

    def _budget(self, resource: str) -> _Budget:
        return self._state.budgets.setdefault(resource, _Budget())

    async def _http(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None:
                token = await self._token()
                self._client = httpx.AsyncClient(
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {token}",
                    },
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                    transport=self._transport,
                )
            return self._client

    async def _send(
        self,
        method: str,
        url: str,
        resource: str,
        caller: str,
        **kwargs,
    ) -> httpx.Response:
        calls = self._calls(caller)
        wait = self.blocked(resource)
        if wait > 0:
            calls.rate_limited += 1
            raise RateLimited(resource, wait)
        client = await self._http()
        calls.requests += 1
        response = await client.request(method, url, **kwargs)
        self._record(resource, response)
        if response.status_code == 304:
            calls.not_modified += 1
            return response
        if _rate_limited(response):
            raise self._exhausted(resource, response, caller)
        if response.is_error:
            calls.failed += 1
            failure = GithubUnavailable if response.status_code >= 500 else RuntimeError
            raise failure(
                f"GitHub {method} {url} failed: {response.status_code} "
                f"{response.text[:200]}"
            )
        self._budget(resource).backoff = 0.0
        return response

    def _record(self, resource: str, response: httpx.Response) -> None:
        """Takes the budget's figures from the response headers, and blocks it
        when that was the last request it allowed."""
        budget = self._budget(resource)
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            budget.remaining = int(remaining)
            budget.limit = int(response.headers.get("x-ratelimit-limit", 0)) or None
        reset = response.headers.get("x-ratelimit-reset")
        if reset is not None:
            budget.reset = datetime.fromtimestamp(int(reset), UTC)
        if budget.remaining == 0 and budget.reset is not None:
            self._block(budget, (budget.reset - self._wall_clock()).total_seconds())

    def _block(self, budget: _Budget, wait: float) -> None:
        budget.blocked_until = self._clock() + max(wait, 1.0)

    def _exhausted(
        self, resource: str, response: httpx.Response, caller: str
    ) -> RateLimited:
        """Blocks ``resource`` and returns the error to raise: until GitHub's
        reset time when it gave one, else for a growing wait."""
        self._calls(caller).rate_limited += 1
        budget = self._budget(resource)
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            wait = float(retry_after)
        elif budget.reset is not None and budget.reset > self._wall_clock():
            wait = (budget.reset - self._wall_clock()).total_seconds()
        else:
            budget.backoff = min(
                max(budget.backoff * 2, BACKOFF_SECONDS), MAX_BACKOFF_SECONDS
            )
            wait = budget.backoff
        self._block(budget, wait)
        logger.warning(
            "GitHub %s rate limit hit by %s: waiting %.0fs", resource, caller, wait
        )
        return RateLimited(resource, wait)


def _is_rate_limit(error: dict) -> bool:
    return (
        error.get("type") == "RATE_LIMIT" or error.get("code") == "graphql_rate_limit"
    )


def _rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    return response.status_code == 403 and (
        response.headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in response.text.lower()
    )
