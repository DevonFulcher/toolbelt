"""The central GitHub client: what it sends, how it reads the budget off the
response headers, how it stops every caller when a budget runs out, and what it
counts per caller. The transport is `httpx.MockTransport`: nothing here reaches
GitHub or runs `gh`."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from toolbelt.ui.github_requests import (
    BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    ConditionalAnswer,
    GithubRequests,
    RateLimited,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def reset_in(seconds: float) -> str:
    return str(int((NOW + timedelta(seconds=seconds)).timestamp()))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Server:
    """Answers each request with the next queued response."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []

    def queue(self, status: int = 200, json_body=None, **headers: str) -> None:
        self.responses.append(
            httpx.Response(
                status,
                json=json_body if json_body is not None else {},
                headers=headers,
            )
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def client(server: Server, clock: Clock | None = None) -> GithubRequests:
    clock = clock or Clock()

    async def token() -> str:
        return "secret"

    return GithubRequests(
        token=token,
        transport=httpx.MockTransport(server),
        clock=clock,
        wall_clock=lambda: NOW + timedelta(seconds=clock.now - 1000),
    )


def run(coro):
    return asyncio.run(coro)


def test_a_graphql_request_is_authenticated_and_returns_the_body():
    server = Server()
    server.queue(json_body={"data": {"viewer": {"login": "devon"}}})
    github = client(server)

    body = run(
        github.graphql("query { viewer { login } }", {"a": "b"}, caller="status")
    )

    assert body == {"data": {"viewer": {"login": "devon"}}}
    (request,) = server.requests
    assert str(request.url) == "https://api.github.com/graphql"
    assert request.headers["authorization"] == "Bearer secret"
    assert json.loads(request.content) == {
        "query": "query { viewer { login } }",
        "variables": {"a": "b"},
    }


def test_a_graphql_response_with_errors_raises_unless_the_partial_field_has_data():
    server = Server()
    server.queue(json_body={"data": None, "errors": [{"message": "nope"}]})
    server.queue(
        json_body={"data": {"search": {"nodes": [1]}}, "errors": [{"message": "SAML"}]}
    )
    server.queue(json_body={"data": {"search": None}, "errors": [{"message": "SAML"}]})
    github = client(server)

    with pytest.raises(RuntimeError, match="nope"):
        run(github.graphql("q", {}, caller="status"))
    partial = run(github.graphql("q", {}, caller="untracked", partial="search"))
    assert partial["data"] == {"search": {"nodes": [1]}}
    with pytest.raises(RuntimeError, match="SAML"):
        run(github.graphql("q", {}, caller="untracked", partial="search"))


def test_a_conditional_get_sends_the_etag_and_reads_a_304_and_a_new_etag():
    server = Server()
    server.queue(etag='"v1"')
    server.queue(304, etag='"v1"')
    server.queue(etag='"v2"')
    github = client(server)

    first = run(github.conditional_get("repos/o/r/pulls/5", None, caller="gate"))
    same = run(github.conditional_get("repos/o/r/pulls/5", '"v1"', caller="gate"))
    changed = run(github.conditional_get("repos/o/r/pulls/5", '"v1"', caller="gate"))

    assert first == ConditionalAnswer(changed=True, etag='"v1"')
    assert same == ConditionalAnswer(changed=False, etag='"v1"')
    assert changed == ConditionalAnswer(changed=True, etag='"v2"')
    assert "if-none-match" not in server.requests[0].headers
    assert server.requests[1].headers["if-none-match"] == '"v1"'


def test_an_error_status_raises():
    server = Server()
    server.queue(404, json_body={"message": "Not Found"})

    with pytest.raises(RuntimeError, match="404"):
        run(client(server).get_json("repos/o/missing", caller="admin"))


class TestBudget:
    def test_the_budget_is_read_off_the_headers(self):
        server = Server()
        server.queue(
            **{
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "4990",
                "x-ratelimit-reset": reset_in(600),
            }
        )
        github = client(server)

        run(github.get_json("repos/o/r", caller="admin"))

        core = github.usage()["budgets"]["core"]
        assert (core["limit"], core["remaining"]) == (5000, 4990)
        assert core["reset"] == (NOW + timedelta(seconds=600)).isoformat()
        assert core["blocked_for"] == 0

    def test_a_graphql_rate_limit_error_blocks_graphql_until_its_reset(self):
        server = Server()
        server.queue(
            json_body={"errors": [{"type": "RATE_LIMIT", "message": "exceeded"}]},
            **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset_in(300)},
        )
        clock = Clock()
        github = client(server, clock)

        with pytest.raises(RateLimited) as raised:
            run(github.graphql("q", {}, caller="status"))
        assert raised.value.resource == "graphql"

        # Every GraphQL caller now stops without sending anything...
        with pytest.raises(RateLimited):
            run(github.graphql("q", {}, caller="aggregate"))
        assert len(server.requests) == 1
        assert github.usage()["callers"]["aggregate"]["rate_limited"] == 1
        # ...but REST has its own budget.
        server.queue(304)
        assert (
            run(
                github.conditional_get("repos/o/r/pulls/5", '"v"', caller="gate")
            ).changed
            is False
        )

        clock.now += 301
        server.queue(json_body={"data": {}})
        run(github.graphql("q", {}, caller="status"))
        assert len(server.requests) == 3

    def test_the_last_allowed_request_succeeds_and_blocks_the_next(self):
        server = Server()
        server.queue(
            json_body={"data": {}},
            **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset_in(120)},
        )
        github = client(server)

        run(github.graphql("q", {}, caller="status"))

        assert 119 <= github.blocked("graphql") <= 120
        with pytest.raises(RateLimited):
            run(github.graphql("q", {}, caller="status"))

    def test_a_403_that_names_the_rate_limit_blocks_with_a_growing_wait(self):
        server = Server()
        for _ in range(8):
            server.queue(403, json_body={"message": "API rate limit exceeded"})
        clock = Clock()
        github = client(server, clock)
        waits = []

        for _ in range(8):
            with pytest.raises(RateLimited):
                run(github.get_json("repos/o/r", caller="admin"))
            waits.append(round(github.blocked("core")))
            clock.now += waits[-1] + 1

        assert waits[:3] == [BACKOFF_SECONDS, BACKOFF_SECONDS * 2, BACKOFF_SECONDS * 4]
        assert waits[-1] == MAX_BACKOFF_SECONDS

    def test_retry_after_sets_the_wait(self):
        server = Server()
        server.queue(429, **{"retry-after": "17"})
        github = client(server)

        with pytest.raises(RateLimited) as raised:
            run(github.get_json("repos/o/r", caller="admin"))

        assert raised.value.wait == 17

    def test_a_success_resets_the_backoff(self):
        server = Server()
        server.queue(403, json_body={"message": "rate limit"})
        server.queue()
        server.queue(403, json_body={"message": "rate limit"})
        clock = Clock()
        github = client(server, clock)

        with pytest.raises(RateLimited):
            run(github.get_json("repos/o/r", caller="admin"))
        clock.now += 100
        run(github.get_json("repos/o/r", caller="admin"))
        with pytest.raises(RateLimited):
            run(github.get_json("repos/o/r", caller="admin"))

        assert round(github.blocked("core")) == BACKOFF_SECONDS


def test_usage_counts_what_each_caller_asked():
    server = Server()
    server.queue(json_body={"data": {}})
    server.queue(304)
    server.queue(404, json_body={"message": "Not Found"})
    github = client(server)

    run(github.graphql("q", {}, caller="status"))
    run(github.conditional_get("repos/o/r/pulls/5", '"v"', caller="gate"))
    with pytest.raises(RuntimeError):
        run(github.get_json("repos/o/x", caller="admin"))

    callers = github.usage()["callers"]
    assert callers["status"] == {
        "requests": 1,
        "not_modified": 0,
        "rate_limited": 0,
        "failed": 0,
    }
    assert callers["gate"]["not_modified"] == 1
    assert callers["admin"]["failed"] == 1


class TestCallers:
    """The UI's callers send what GitHub expects, through the client."""

    def graphql_server(self, body: dict) -> Server:
        server = Server()
        server.queue(json_body=body)
        return server

    def sent(self, server: Server) -> dict:
        return json.loads(server.requests[0].content)

    def test_the_pr_lookup_asks_for_the_checkouts_repo_and_the_branch(self):
        from pathlib import Path

        from toolbelt.ui.pr_fetch import PR_QUERY, graphql_runner

        server = self.graphql_server({"data": {"repository": None}})

        async def slug_of(path: Path) -> str | None:
            return "dbt-labs/api"

        run_pr = graphql_runner(client(server), slug_of)

        run(run_pr("devon/a", Path("/repo")))

        assert self.sent(server) == {
            "query": PR_QUERY,
            "variables": {"owner": "dbt-labs", "repo": "api", "branch": "devon/a"},
        }

    def test_the_pr_lookup_of_a_checkout_without_a_github_remote_fails(self):
        from pathlib import Path

        from toolbelt.ui.pr_fetch import graphql_runner

        async def slug_of(path: Path) -> str | None:
            return None

        run_pr = graphql_runner(client(Server()), slug_of)

        with pytest.raises(RuntimeError, match="no GitHub remote"):
            run(run_pr("devon/a", Path("/repo")))

    def test_the_untracked_search_and_the_history_pages_name_their_callers(self):
        from toolbelt.ui.aggregate_feed import page_runner
        from toolbelt.ui.untracked_prs import SEARCH_QUERY, search_runner

        server = Server()
        server.queue(
            json_body={
                "data": {"search": {"nodes": [1]}},
                "errors": [{"message": "SAML"}],
            }
        )
        server.queue(json_body={"data": {"search": {"nodes": []}}})
        github = client(server)

        run(search_runner(github)())
        run(page_runner(github)("is:pr author:@me", "cursor"))

        assert self.sent(server)["variables"] == {"search": SEARCH_QUERY}
        assert json.loads(server.requests[1].content)["variables"] == {
            "search": "is:pr author:@me",
            "after": "cursor",
        }
        callers = github.usage()["callers"]
        assert callers["untracked"]["requests"] == 1
        assert callers["aggregate"]["requests"] == 1
