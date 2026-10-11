"""The "Open PRs not tracked by toolbelt" callout: the pure matching (which of
the user's open PRs are not stack branches), the slow refresh feed and its
place in the snapshot and event stream. The `gh` search and the remote lookup
are injected: nothing here touches GitHub."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from toolbelt.ui import api_models
from toolbelt.ui.app import create_app
from toolbelt.ui.live_status import EventHub
from toolbelt.ui.untracked_prs import (
    OpenPr,
    TrackedRepo,
    UntrackedPrFeed,
    parse_search,
    repo_slug,
    tracked_pairs,
    untracked,
)

from test_ui import FakeStatuses, seed_repo


def node(
    number: int,
    repo: str = "o/api",
    head: str | None = None,
    *,
    archived: bool = False,
    draft: bool = False,
    title: str | None = None,
) -> dict:
    return {
        "number": number,
        "title": title or f"PR {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "createdAt": "2026-10-01T09:00:00Z",
        "isDraft": draft,
        "headRefName": head or f"branch-{number}",
        "repository": {"nameWithOwner": repo, "isArchived": archived},
    }


def payload(*nodes: dict, count: int | None = None) -> dict:
    # The search spans issues and PRs; a node that is not a PR is empty.
    return {
        "data": {"search": {"issueCount": count or len(nodes), "nodes": [*nodes, {}]}}
    }


class TestRepoSlug:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/Owner/Repo.git",
            "https://github.com/Owner/Repo",
            "git@github.com:Owner/Repo.git",
            "ssh://git@github.com/Owner/Repo.git",
            "https://github.com/Owner/Repo/",
        ],
    )
    def test_github_urls_become_lower_case_owner_and_name(self, url):
        assert repo_slug(url) == "owner/repo"

    def test_nothing_to_resolve(self):
        assert repo_slug(None) is None
        assert repo_slug("") is None
        assert repo_slug("not a remote") is None


class TestParseSearch:
    def test_reads_the_fields_and_skips_non_pr_nodes(self):
        search = parse_search(
            payload(node(7, "o/api", "devon/x", draft=True, title="Fix it"), count=12)
        )

        assert search.total == 12
        [pr] = search.prs
        assert (pr.repo, pr.number, pr.title, pr.head_branch) == (
            "o/api",
            7,
            "Fix it",
            "devon/x",
        )
        assert pr.is_draft and not pr.archived
        assert pr.created_at == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

    def test_prs_github_would_not_show_are_counted_not_listed(self):
        # A SAML-protected organization the token is not authorized for comes
        # back as FORBIDDEN errors, with a null node per hidden PR.
        body = payload(node(1), count=3)
        body["data"]["search"]["nodes"] = [node(1), None, None]
        body["errors"] = [
            {"type": "FORBIDDEN", "path": ["search", "nodes", 1]},
            {"type": "FORBIDDEN", "path": ["search", "nodes", 2]},
        ]

        search = parse_search(body)

        assert [pr.number for pr in search.prs] == [1]
        assert (search.total, search.hidden) == (3, 2)


def open_pr(number: int, repo: str = "o/api", head: str = "b", **kw) -> OpenPr:
    return OpenPr(
        repo=repo,
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/{repo}/pull/{number}",
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        is_draft=False,
        head_branch=head,
        archived=kw.get("archived", False),
    )


class TestUntracked:
    def test_a_pr_of_a_tracked_branch_is_left_out(self):
        prs = [open_pr(1, head="devon/a"), open_pr(2, head="devon/other")]

        got = untracked(prs, {("o/api", "devon/a")})

        assert [pr.number for pr in got] == [2]

    def test_the_branch_must_be_tracked_in_the_same_repo(self):
        prs = [open_pr(1, "o/web", "devon/a")]

        got = untracked(prs, {("o/api", "devon/a")})

        assert [pr.number for pr in got] == [1]

    def test_repo_names_compare_without_case(self):
        prs = [open_pr(1, "Owner/API", "devon/a")]

        assert untracked(prs, {("owner/api", "devon/a")}) == []

    def test_prs_in_archived_repos_are_left_out(self):
        prs = [open_pr(1, archived=True), open_pr(2)]

        assert [pr.number for pr in untracked(prs, set())] == [2]

    def test_ordered_by_repo_then_newest_first(self):
        prs = [open_pr(3, "o/zeta"), open_pr(1, "o/api"), open_pr(9, "o/api")]

        assert [(pr.repo, pr.number) for pr in untracked(prs, set())] == [
            ("o/api", 9),
            ("o/api", 1),
            ("o/zeta", 3),
        ]


class TestTrackedPairs:
    def test_uses_the_stored_remote_before_asking_the_checkout(self):
        asked: list[Path] = []

        async def remote_of(path: Path) -> str | None:
            asked.append(path)
            return "git@github.com:o/fallback.git"

        repos = [
            TrackedRepo(
                Path("/a"), "https://github.com/o/stored.git", frozenset({"x"})
            ),
            TrackedRepo(Path("/b"), None, frozenset({"y", "z"})),
            TrackedRepo(Path("/c"), None, frozenset({"lost"})),
        ]

        async def remotes(path: Path) -> str | None:
            return None if path == Path("/c") else await remote_of(path)

        pairs = asyncio.run(tracked_pairs(repos, remotes))

        assert pairs == {("o/stored", "x"), ("o/fallback", "y"), ("o/fallback", "z")}
        assert asked == [Path("/b")]


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_feed(result, repos=None, clock=None, **kwargs):
    calls = []

    async def search() -> dict:
        calls.append(1)
        if isinstance(result, Exception):
            raise result
        return result

    async def remote_of(path: Path) -> str | None:
        return None

    hub = EventHub()
    feed = UntrackedPrFeed(
        repos=lambda: repos or [],
        search=search,
        hub=hub,
        remote_of=remote_of,
        clock=clock or Clock(),
        interval=180,
        **kwargs,
    )
    return feed, hub, calls


class TestFeed:
    def test_is_loading_until_the_first_refresh(self):
        feed, _, _ = make_feed(payload())

        assert feed.get() == api_models.UntrackedLoading(state="loading")
        assert feed.is_due()

    def test_lists_untracked_prs_and_publishes_the_change_once(self):
        repos = [
            TrackedRepo(Path("/a"), "git@github.com:o/api.git", frozenset({"devon/a"}))
        ]
        feed, hub, _ = make_feed(
            payload(
                node(1, "o/api", "devon/a"), node(2, "o/api", "devon/new", draft=True)
            ),
            repos,
        )

        async def scenario():
            queue = hub.subscribe()
            first = await feed.run_once()
            second = await feed.run_once()  # nothing changed
            return first, second, queue.qsize(), queue.get_nowait()

        first, second, published, event = asyncio.run(scenario())

        assert (first, second, published) == (True, False, 1)
        assert event[0] == "untracked"
        listed = feed.get()
        assert isinstance(listed, api_models.UntrackedLoaded)
        assert [(p.repo, p.number, p.is_draft) for p in listed.prs] == [
            ("o/api", 2, True)
        ]
        assert listed.prs[0].created_at == "2026-10-01T09:00:00Z"
        assert not listed.truncated
        # The event carries a valid member of the typed stream.
        TypeAdapter(api_models.ServerEvent).validate_python(
            {"type": "untracked", "v": 1, **event[1]}
        )

    def test_a_failed_search_is_reported_not_raised(self):
        feed, hub, _ = make_feed(RuntimeError("gh api graphql failed: no auth"))

        asyncio.run(feed.run_once())

        assert feed.get() == api_models.UntrackedFailed(
            state="failed", error="gh api graphql failed: no auth"
        )

    def test_refreshes_only_when_the_interval_has_passed(self):
        clock = Clock()
        feed, _, calls = make_feed(payload(), clock=clock)

        async def scenario():
            await feed.run_once()
            clock.now += 179
            due_early = feed.is_due()
            clock.now += 1
            return due_early, feed.is_due()

        assert asyncio.run(scenario()) == (False, True)
        assert len(calls) == 1


class TestInTheApp:
    def client(self, result, fetcher: FakeStatuses, **kwargs) -> TestClient:
        calls: list[int] = []

        async def search() -> dict:
            calls.append(1)
            return result

        async def remote_of(path: Path) -> str | None:
            return "git@github.com:o/api.git"

        app = create_app(
            fetch=fetcher,
            search_open_prs=search,
            remote_of=remote_of,
            tracker=lambda: None,
            **kwargs,
        )
        client = TestClient(app)
        client.calls = calls  # type: ignore[attr-defined]
        return client

    def test_the_snapshot_carries_the_list_and_never_fetches_it(self, tmp_path):
        seed_repo(tmp_path, "api", {"devon/a": "main"})
        client = self.client(
            payload(node(1, "o/api", "devon/a"), node(2, "o/other", "x")),
            FakeStatuses(),
        )

        before = client.get("/api/snapshot").json()
        assert before["untracked"] == {"state": "loading"}
        assert client.calls == []  # type: ignore[attr-defined]

        asyncio.run(client.app.state.untracked_feed.run_once())  # type: ignore[attr-defined]
        after = client.get("/api/snapshot").json()

        assert after["untracked"]["state"] == "loaded"
        # devon/a is tracked in o/api (its remote is resolved from the
        # checkout); the PR in a repo toolbelt does not know is listed.
        assert [p["number"] for p in after["untracked"]["prs"]] == [2]
        api_models.Snapshot.model_validate(after)
