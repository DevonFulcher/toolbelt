"""`tt ui`'s JSON API, driven through FastAPI's TestClient.

The status fetcher and the task tracker are injected into `create_app`, so no
test touches `gh`, Jira or the real config. Polling is driven a pass at a time
(`refresh`), never by the background loop; the live event stream is tested in
test_ui_live.py. The last test starts the real CLI command under uvicorn.
"""

import asyncio
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import git

from toolbelt.git.stack import lineage
from toolbelt.git.stack.status import (
    NO_PR,
    BranchStatus,
    CiState,
    PrState,
    ReviewState,
)
from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.tasks.tracker import TaskInfo
from toolbelt.ui import api_models, pr_states, snapshot
from toolbelt.ui.app import create_app
from toolbelt.ui.branch_links import agent_link, owning_tracker, task_link
from toolbelt.ui.pr_activity import PrFacts, TimelineItem
from toolbelt.ui.pr_fetch import PrSnapshot

AGENT_LINK = "claude://claude.ai/epitaxy/local_0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


class FakeStatuses:
    """A `StatusFetcher` serving seeded statuses (default: no PR)."""

    def __init__(self) -> None:
        self.statuses: dict[str, BranchStatus] = {}
        self.facts: dict[str, PrFacts] = {}
        self.calls: list[tuple[str, Path]] = []
        self.fail = False

    async def __call__(self, branch: str, root: Path) -> PrSnapshot:
        self.calls.append((branch, root))
        if self.fail:
            raise FileNotFoundError("gh")
        return PrSnapshot(self.statuses.get(branch, NO_PR), self.facts.get(branch))


class FakeTracker:
    """A `TaskTracker` serving seeded `TaskInfo`; unseeded refs fail to fetch."""

    name = "fake"

    def __init__(self) -> None:
        self.tasks: dict[str, TaskInfo] = {}
        self.fetches: list[str] = []
        self.reason: str | None = None

    def unavailable_reason(self) -> str | None:
        return self.reason

    def url_for(self, ref: str) -> str | None:
        return f"https://tasks.example/{ref}"

    def display_ref(self, ref: str) -> str:
        return ref.upper()

    def fetch(self, ref: str) -> TaskInfo | None:
        self.fetches.append(ref)
        return self.tasks.get(ref)


def seed_repo(
    root: Path,
    name: str,
    parents: dict[str, str],
    *,
    links: dict[str, tuple[str | None, str | None]] | None = None,
    task_tracker: str | None = "fake",
    started: dict[str, datetime] | None = None,
    exists: bool = True,
) -> int:
    path = root / name
    if exists:
        path.mkdir(parents=True)
    store = StackStore(default_db_path())
    try:
        repo_id = store.ensure_repo(
            RepoIdentity(git_common_dir=path / ".git", path=path, remote_url=None)
        )
        for branch, parent in parents.items():
            task, agent = (links or {}).get(branch, (None, None))
            store.set_parent(
                repo_id,
                branch,
                parent,
                task=task,
                task_tracker=task_tracker if task else None,
                agent_link=agent,
                started_at=(started or {}).get(branch),
            )
        return repo_id
    finally:
        store.close()


@pytest.fixture
def fetcher() -> FakeStatuses:
    return FakeStatuses()


@pytest.fixture
def tracker() -> FakeTracker:
    return FakeTracker()


@pytest.fixture
def make_client(fetcher: FakeStatuses):
    def make(
        task_tracker: FakeTracker | None = None,
        cwd_root: Path | None = None,
        **app_kwargs,
    ) -> TestClient:
        app_kwargs.setdefault("wall_clock", lambda: NOW)
        app = create_app(
            cwd_root, fetch=fetcher, tracker=lambda: task_tracker, **app_kwargs
        )
        return TestClient(app)

    return make


def refresh(client: TestClient) -> int:
    """Run one polling pass, as the background loop would."""
    return asyncio.run(client.app.state.refresher.run_once())  # type: ignore[attr-defined]


def refresh_tasks(client: TestClient) -> int:
    """Run one task-feed pass, as the background loop would."""
    return asyncio.run(client.app.state.task_feed.run_once())  # type: ignore[attr-defined]


def snap(client: TestClient) -> dict:
    response = client.get("/api/snapshot")
    assert response.status_code == 200
    return response.json()


def branches(client: TestClient) -> dict[str, dict]:
    return {b["name"]: b for b in snap(client)["branches"]}


def status_of(client: TestClient, name: str) -> dict:
    return branches(client)[name]["status"]


def open_status(**overrides) -> BranchStatus:
    base: dict = dict(
        pr=PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.NONE,
        url="https://github.com/o/r/pull/5",
    )
    base.update(overrides)
    return BranchStatus(**base)


# ---- the snapshot ---------------------------------------------------------------


def test_snapshot_lists_every_branch_depth_first_without_fetching_status(
    tmp_path, make_client, fetcher
):
    seed_repo(
        tmp_path,
        "api",
        {"devon/a": "main", "devon/b": "devon/a", "devon/c": "devon/a"},
    )

    data = snap(make_client())

    assert [b["name"] for b in data["branches"]] == ["devon/a", "devon/b", "devon/c"]
    # Rendering never waits on `gh`: every status is "loading" until the
    # background refresher has an answer.
    assert fetcher.calls == []
    assert {b["status"]["load"] for b in data["branches"]} == {"loading"}


def test_the_snapshot_validates_against_the_wire_models(tmp_path, make_client, fetcher):
    seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "devon/a"})
    fetcher.statuses["devon/a"] = open_status(open_comments=2, has_conflicts=True)
    client = make_client()
    refresh(client)

    parsed = api_models.Snapshot.model_validate(snap(client))

    assert [b.name for b in parsed.branches] == ["devon/a", "devon/b"]
    assert parsed.catalog.events.opened.glyph == "◆"


def test_branches_carry_tree_data_for_the_client(tmp_path, make_client):
    repo_id = seed_repo(
        tmp_path,
        "api",
        {"devon/a": "main", "devon/b": "devon/a", "devon/c": "main"},
    )

    data = snap(make_client())

    r = f"{repo_id}:"
    # The trunk is not a branch: a stack starts at its first branch, depth 0.
    assert [
        (
            b["id"],
            b["parent_id"],
            b["order"],
            b["depth"],
            b["stack_pos"],
            b["stack_height"],
        )
        for b in data["branches"]
    ] == [
        (r + "devon/a", None, 0, 0, 1, 2),
        (r + "devon/b", r + "devon/a", 1, 1, 2, 2),
        (r + "devon/c", None, 2, 0, 1, 1),
    ]
    a = data["branches"][0]
    assert a["children"] == [{"id": r + "devon/b", "name": "devon/b"}]
    assert a["parent"] is None
    assert data["branches"][1]["parent"] == "devon/a"
    assert "main" not in {b["name"] for b in data["branches"]}


def test_branches_carry_when_their_task_started(tmp_path, make_client):
    seed_repo(
        tmp_path,
        "api",
        {"devon/a": "main", "devon/b": "main"},
        started={"devon/a": datetime(2026, 9, 28, 14, 30, tzinfo=UTC)},
    )

    data = snap(make_client())

    started = {b["name"]: b["started_at"] for b in data["branches"]}
    assert started["devon/a"] == "2026-09-28T14:30:00Z"
    assert started["devon/b"] is not None  # registered now


def test_filters_list_every_value_with_friendly_next_labels(tmp_path, make_client):
    seed_repo(tmp_path, "alpha", {"devon/one": "main"})

    filters = {f["key"]: f for f in snap(make_client())["filters"]}

    assert {key: [o["value"] for o in f["options"]] for key, f in filters.items()} == {
        "next": ["you", "ci", "others", "none"],
        "pr": ["none", "draft", "open", "merged", "closed"],
        "ci": ["none", "running", "failed", "success"],
        "review": ["none", "requested", "approved", "reviewed"],
        "comments": ["has comments", "none"],
        "merge": ["conflicts", "clean", "none"],
    }
    assert [(o["value"], o["label"]) for o in filters["next"]["options"]] == [
        ("you", "Needs me"),
        ("ci", "Waiting on CI"),
        ("others", "Waiting on others"),
        ("none", "Done"),
    ]


def test_multi_repo_is_grouped_by_repo_name(tmp_path, make_client):
    # Seeded out of name order: branches still group by repo, sorted by name.
    seed_repo(tmp_path, "beta", {"devon/two": "develop"})
    seed_repo(tmp_path, "alpha", {"devon/one": "main", "devon/uno": "devon/one"})

    data = snap(make_client())

    assert [b["name"] for b in data["branches"]] == [
        "devon/one",
        "devon/uno",
        "devon/two",
    ]
    assert [r["name"] for r in data["repos"]] == ["alpha", "beta"]
    assert [b["order"] for b in data["branches"]] == [0, 1, 2]


def test_same_branch_name_in_two_repos_has_distinct_ids(tmp_path, make_client):
    seed_repo(tmp_path, "alpha", {"devon/x": "main"})
    seed_repo(tmp_path, "beta", {"devon/x": "main"})

    ids = [b["id"] for b in snap(make_client())["branches"]]

    assert len(ids) == len(set(ids)) == 2


def test_no_repos(make_client):
    data = snap(make_client())

    assert data["repos"] == [] and data["branches"] == []


def test_missing_repo_path_is_flagged_and_never_polled(tmp_path, make_client, fetcher):
    seed_repo(tmp_path, "gone", {"devon/x": "main"}, exists=False)
    client = make_client()

    data = snap(client)
    assert data["repos"][0]["path_missing"] is True
    assert data["branches"][0]["status"] == {"load": "loading"}

    assert refresh(client) == 0
    assert fetcher.calls == []


def test_the_snapshot_carries_the_event_version_it_was_read_at(tmp_path, make_client):
    seed_repo(tmp_path, "api", {"devon/a": "main"})
    client = make_client()
    client.app.state.hub.publish("structure", {})  # type: ignore[attr-defined]

    assert snap(client)["version"] == 1


def test_the_catalog_in_the_snapshot_is_the_state_catalog(make_client):
    catalog = snap(make_client())["catalog"]

    assert catalog["events"]["review_approved"]["glyph"] == "✓"
    assert set(catalog["events"]) == set(pr_states.EVENTS)
    assert set(catalog["phases"]) == set(pr_states.PHASES)
    assert {(s["dimension"], s["value"]) for s in catalog["states"]} == {
        (s.dimension, s.value) for s in pr_states.STATES
    }


# ---- status -------------------------------------------------------------------


def test_status_shows_pr_ci_review_comments_conflicts(tmp_path, make_client, fetcher):
    seed_repo(tmp_path, "api", {"devon/a": "main"})
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.FAILED,
        review=ReviewState.APPROVED,
        url="https://github.com/o/r/pull/42",
        open_comments=2,
        has_conflicts=True,
    )
    client = make_client()

    assert refresh(client) == 1

    assert fetcher.calls == [("devon/a", tmp_path / "api")]
    status = status_of(client, "devon/a")
    assert status["load"] == "loaded"
    assert (status["pr"], status["pr_number"]) == ("open", 42)
    assert status["pr_url"] == "https://github.com/o/r/pull/42"
    assert (status["ci"], status["review"]) == ("failed", "approved")
    assert (status["open_comments"], status["merge"]) == (2, "conflicts")


def test_draft_and_no_pr_status(tmp_path, make_client, fetcher):
    seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "devon/a"})
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.DRAFT,
        ci=CiState.NONE,
        review=ReviewState.NONE,
        url="https://github.com/o/r/pull/7",
    )
    client = make_client()
    refresh(client)

    draft, none = status_of(client, "devon/a"), status_of(client, "devon/b")

    assert (draft["pr"], draft["pr_number"]) == ("draft", 7)
    assert draft["ci"] == "none"
    # A draft PR with no conflicts is mergeable.
    assert (draft["merge"], draft["open_comments"]) == ("clean", 0)
    assert (none["pr"], none["pr_number"], none["pr_url"]) == ("none", None, None)
    assert (none["merge"], none["open_comments"]) == ("none", 0)
    assert none["detail"] is None


def test_a_failed_lookup_is_reported_as_failed(tmp_path, make_client, fetcher):
    seed_repo(tmp_path, "api", {"devon/a": "main"})
    fetcher.fail = True
    client = make_client()

    refresh(client)

    assert status_of(client, "devon/a") == {"load": "failed"}


def test_next_says_what_to_do_and_who_it_waits_on(tmp_path, make_client, fetcher):
    seed_repo(
        tmp_path,
        "api",
        {"devon/none": "main", "devon/ci": "main", "devon/ready": "main"},
    )
    pull = "https://github.com/o/r/pull/"
    fetcher.statuses["devon/ci"] = open_status(ci=CiState.RUNNING, url=pull + "1")
    fetcher.statuses["devon/ready"] = open_status(
        review=ReviewState.APPROVED, url=pull + "2"
    )
    client = make_client()
    refresh(client)

    got = {name: status_of(client, name)["next"] for name in branches(client)}

    assert got["devon/none"] == {"label": "Open PR", "who": "you", "kind": "open_pr"}
    assert got["devon/ci"] == {
        "label": "Waiting on CI",
        "who": "ci",
        "kind": "wait_ci",
    }
    assert got["devon/ready"] == {"label": "Merge", "who": "you", "kind": "merge"}


def test_a_stacked_branch_is_blocked_until_its_parent_merges(
    tmp_path, make_client, fetcher
):
    seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "devon/a"})
    pull = "https://github.com/o/r/pull/"
    fetcher.statuses["devon/a"] = open_status(
        review=ReviewState.REQUESTED, url=pull + "1"
    )
    fetcher.statuses["devon/b"] = open_status(
        review=ReviewState.APPROVED, url=pull + "2"
    )
    now = [1000.0]
    client = make_client(clock=lambda: now[0])
    refresh(client)

    blocked = status_of(client, "devon/b")["next"]
    assert (blocked["label"], blocked["who"]) == ("Blocked on devon/a", "others")

    # Once the parent merges, the child can be merged.
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.MERGED,
        ci=CiState.SUCCESS,
        review=ReviewState.APPROVED,
        url=pull + "1",
    )
    now[0] += 100  # past the polling interval
    refresh(client)
    assert status_of(client, "devon/b")["next"]["label"] == "Merge"


def test_blocked_stacks_wait_on_you_when_the_parent_needs_you(
    tmp_path, make_client, fetcher
):
    seed_repo(
        tmp_path,
        "api",
        {
            "devon/a": "main",
            "devon/b": "devon/a",
            "devon/c": "devon/b",
            "devon/x": "main",
            "devon/y": "devon/x",
        },
    )
    pull = "https://github.com/o/r/pull/"
    approved = open_status(review=ReviewState.APPROVED, url=pull + "9")
    # a needs me (CI failed); b and c are approved and stacked on it.
    fetcher.statuses["devon/a"] = open_status(ci=CiState.FAILED, url=pull + "1")
    fetcher.statuses["devon/b"] = approved
    fetcher.statuses["devon/c"] = approved
    # x waits on a reviewer; y is approved on it.
    fetcher.statuses["devon/x"] = open_status(
        review=ReviewState.REQUESTED, url=pull + "2"
    )
    fetcher.statuses["devon/y"] = approved
    client = make_client()
    refresh(client)

    for branch, parent, who in [
        ("devon/b", "devon/a", "you"),
        ("devon/c", "devon/b", "you"),
        ("devon/y", "devon/x", "others"),
    ]:
        action = status_of(client, branch)["next"]
        assert action["label"] == f"Blocked on {parent}"
        assert action["who"] == who


def test_merge_is_only_meaningful_for_open_prs():
    def merge(pr, conflicts):
        return snapshot.loaded_status(
            BranchStatus(
                pr=pr,
                ci=CiState.NONE,
                review=ReviewState.NONE,
                url="https://github.com/o/r/pull/5",
                has_conflicts=conflicts,
            ),
            parent=None,
            facts=None,
            now=NOW,
        ).merge

    assert merge(PrState.OPEN, True) == "conflicts"
    assert merge(PrState.OPEN, False) == "clean"
    assert merge(PrState.MERGED, False) == "none"
    assert merge(PrState.NONE, False) == "none"


def test_a_status_carries_the_catalog_references_of_its_now_chips(
    tmp_path, make_client, fetcher
):
    seed_repo(tmp_path, "api", {"devon/a": "main"})
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.DRAFT,
        ci=CiState.FAILED,
        review=ReviewState.NONE,
        url="https://github.com/o/r/pull/7",
        open_comments=3,
    )
    client = make_client()
    refresh(client)

    chips = status_of(client, "devon/a")["now"]

    assert [(c["dimension"], c["value"]) for c in chips] == [
        ("pr", "draft"),
        ("ci", "failed"),
        ("review", "none"),
        ("comments", "open"),
        ("merge", "clean"),
        ("next", "resolve_comments"),
    ]
    assert chips[3]["n"] == 3
    assert chips[-1] == {
        "dimension": "next",
        "value": "resolve_comments",
        "text": "Resolve comments",
        "who": "you",
    }


# ---- PR detail ---------------------------------------------------------------------


def make_facts(**overrides) -> PrFacts:
    base: dict = dict(
        number=5,
        title="Add <b>it</b>",
        author="devon",
        base_ref="main",
        head_oid="abc",
        state="OPEN",
        is_draft=True,
        created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        merged_at=None,
        closed_at=None,
        requested_reviewers=("sam",),
        commit_rollups=(("a", "FAILURE"), ("b", "SUCCESS"), ("c", "ERROR")),
        items=(
            TimelineItem(
                datetime(2026, 10, 6, 6, 0, tzinfo=UTC),
                "review_requested",
                "devon",
                "requested review from sam",
                subject="sam",
            ),
        ),
    )
    base.update(overrides)
    return PrFacts(**base)


class TestPrDetail:
    def test_ages_failures_and_events_are_exposed_for_display(self):
        detail = snapshot.pr_detail(make_facts(), NOW)

        assert detail.title == "Add <b>it</b>"  # raw: the client escapes
        assert (detail.pr_age.text, detail.pr_age.since) == (
            "5d",
            "2026-10-01T09:00:00Z",
        )
        assert detail.draft_age is not None and detail.draft_age.text == "5d"
        assert detail.review_request_age is not None
        assert (
            detail.review_request_age.text,
            detail.review_request_age.since,
        ) == ("3h", "2026-10-06T06:00:00Z")
        assert detail.ci_failures == 2
        assert detail.reviewers == ["sam"]
        assert (detail.created, detail.ended, detail.is_draft) == (
            "2026-10-01T09:00:00Z",
            None,
            True,
        )
        assert [e.kind for e in detail.events] == ["opened", "review_requested"]
        assert detail.events[1].model_dump() == {
            "t": "2026-10-06T06:00:00Z",
            "kind": "review_requested",
            "actor": "devon",
            "detail": "requested review from sam",
            "ci": "",
            "subject": "sam",
            "ci_done": None,
        }

    def test_a_finished_pr_has_a_fixed_age_that_no_longer_ticks(self):
        merged = make_facts(
            state="MERGED",
            is_draft=False,
            merged_at=datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
            requested_reviewers=(),
        )

        detail = snapshot.pr_detail(merged, NOW)

        assert (detail.pr_age.text, detail.pr_age.since) == ("1d", None)
        assert detail.draft_age is None and detail.review_request_age is None
        assert detail.ended == "2026-10-02T09:00:00Z"

    def test_a_loaded_status_without_facts_has_no_detail(self):
        status = snapshot.loaded_status(open_status(), parent=None, facts=None, now=NOW)

        assert status.detail is None

    def test_the_snapshot_status_carries_the_detail(
        self, tmp_path, make_client, fetcher
    ):
        seed_repo(tmp_path, "api", {"devon/a": "main"})
        fetcher.statuses["devon/a"] = BranchStatus(
            pr=PrState.DRAFT,
            ci=CiState.FAILED,
            review=ReviewState.NONE,
            url="https://github.com/o/r/pull/7",
        )
        fetcher.facts["devon/a"] = make_facts()
        client = make_client()
        refresh(client)

        detail = status_of(client, "devon/a")["detail"]

        assert detail["created"] == "2026-10-01T09:00:00Z"
        assert detail["events"][0]["kind"] == "opened"


# ---- links and tasks ----------------------------------------------------------------


def test_links_go_through_the_tracker(tmp_path, make_client, tracker):
    seed_repo(
        tmp_path,
        "api",
        {"devon/key": "main", "devon/deeplink": "main", "devon/blank": "main"},
        links={
            "devon/key": ("abc-1", None),
            "devon/deeplink": (None, AGENT_LINK),
        },
    )

    got = branches(make_client(tracker))

    assert got["devon/key"]["task"] == {
        "label": "ABC-1",
        "href": "https://tasks.example/abc-1",
    }
    assert got["devon/key"]["task_fetchable"] is True
    # The custom scheme is untouched.
    assert got["devon/deeplink"]["agent"] == {
        "label": "Open session",
        "href": AGENT_LINK,
    }
    assert got["devon/blank"]["task"] is None and got["devon/blank"]["agent"] is None
    # The title/status are fetched by the task feed, never on a request.
    assert tracker.fetches == []


def test_without_a_tracker_a_task_is_plain_text_and_not_fetchable(
    tmp_path, make_client
):
    seed_repo(tmp_path, "api", {"devon/a": "main"}, links={"devon/a": ("abc-1", None)})

    branch = branches(make_client())["devon/a"]

    assert branch["task"] == {"label": "abc-1", "href": None}
    assert branch["task_fetchable"] is False
    assert branch["task_live"] is None


def test_task_of_another_tracker_is_plain_text_and_never_fetched(
    tmp_path, make_client, tracker
):
    seed_repo(
        tmp_path,
        "api",
        {"devon/a": "main"},
        links={"devon/a": ("LIN-5", None)},
        task_tracker="linear",
    )
    client = make_client(tracker)

    branch = branches(client)["devon/a"]
    assert refresh_tasks(client) == 0

    assert branch["task"] == {"label": "LIN-5", "href": None}
    assert branch["task_fetchable"] is False
    assert tracker.fetches == []


class TestTaskFeed:
    def seed(self, tmp_path):
        return seed_repo(
            tmp_path, "api", {"devon/a": "main"}, links={"devon/a": ("abc-1", None)}
        )

    def test_a_task_is_loading_until_the_feed_has_looked_it_up(
        self, tmp_path, make_client, tracker
    ):
        self.seed(tmp_path)
        tracker.tasks["abc-1"] = TaskInfo("ABC-1", "Fix", "In Progress", None)
        client = make_client(tracker)

        assert branches(client)["devon/a"]["task_live"] == {"state": "loading"}
        assert refresh_tasks(client) == 1
        assert branches(client)["devon/a"]["task_live"] == {
            "state": "info",
            "title": "Fix",
            "status": "In Progress",
        }

    def test_the_title_is_published_as_a_task_event(
        self, tmp_path, make_client, tracker
    ):
        repo_id = self.seed(tmp_path)
        tracker.tasks["abc-1"] = TaskInfo("ABC-1", "Fix", "Open", None)
        client = make_client(tracker)
        queue = client.app.state.hub.subscribe()  # type: ignore[attr-defined]

        refresh_tasks(client)
        refresh_tasks(client)  # unchanged: nothing more is published

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert events == [
            (
                "task",
                {
                    "branch_id": f"{repo_id}:devon/a",
                    "task": {"state": "info", "title": "Fix", "status": "Open"},
                    "v": 1,
                },
            )
        ]

    def test_a_failure_says_why(self, tmp_path, make_client, tracker):
        self.seed(tmp_path)
        tracker.reason = "JIRA_API_TOKEN not set"
        client = make_client(tracker)

        refresh_tasks(client)

        assert branches(client)["devon/a"]["task_live"] == {
            "state": "unavailable",
            "reason": "JIRA_API_TOKEN not set",
        }

    def test_a_failure_the_tracker_cannot_explain_still_says_something(
        self, tmp_path, make_client, tracker
    ):
        self.seed(tmp_path)
        client = make_client(tracker)

        refresh_tasks(client)

        live = branches(client)["devon/a"]["task_live"]
        assert live == {"state": "unavailable", "reason": "task details unavailable"}

    def test_lookups_are_cached_with_a_ttl(self, tmp_path, make_client, tracker):
        self.seed(tmp_path)
        tracker.tasks["abc-1"] = TaskInfo("ABC-1", "T", "Open", None)
        now = [100.0]
        client = make_client(tracker, task_ttl_seconds=60, clock=lambda: now[0])

        refresh_tasks(client)
        now[0] += 59
        refresh_tasks(client)
        assert tracker.fetches == ["abc-1"]

        now[0] += 2
        refresh_tasks(client)
        assert tracker.fetches == ["abc-1", "abc-1"]

    def test_only_linked_tasks_of_tracked_branches_reach_the_tracker(
        self, tmp_path, make_client, tracker
    ):
        seed_repo(tmp_path, "api", {"devon/a": "main"})  # no task stored
        client = make_client(tracker)

        assert refresh_tasks(client) == 0
        assert tracker.fetches == []


def test_the_api_is_read_only(tmp_path, make_client):
    seed_repo(tmp_path, "api", {"devon/a": "main"})
    client = make_client()
    for url in ("/", "/api/snapshot", "/api/events"):
        assert client.post(url).status_code == 405
        assert client.delete(url).status_code == 405


# ---- the page shell ---------------------------------------------------------------


def test_the_index_serves_the_built_client(tmp_path, make_client):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>toolbelt</title>")
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)")
    client = make_client(client_dist=dist)

    index = client.get("/")
    asset = client.get("/assets/app-abc123.js")

    assert index.status_code == 200 and "<title>toolbelt" in index.text
    assert index.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200 and asset.text == "console.log(1)"


def test_the_favicon_is_served_at_the_linked_path_and_at_the_implicit_one(
    tmp_path, make_client
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "favicon.svg").write_text("<svg/>")
    client = make_client(client_dist=dist)

    for path in ("/favicon.svg", "/favicon.ico"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"] == "image/svg+xml"
        assert response.text == "<svg/>"


def test_the_favicon_is_404_when_the_client_is_not_built(tmp_path, make_client):
    client = make_client(client_dist=tmp_path / "missing")

    assert client.get("/favicon.ico").status_code == 404


def test_the_page_links_a_favicon_the_client_build_ships():
    web = Path(__file__).resolve().parents[1] / "web"
    page = (web / "index.html").read_text()

    link = re.search(r'<link rel="icon"[^>]*href="/([^"]+)"', page)

    assert link, "index.html has no icon link"
    # Vite copies web/public as is into the build, so the file is served at /<name>.
    assert (web / "public" / link.group(1)).is_file()
    assert "<title>toolbelt factory</title>" in page


def test_an_unbuilt_client_says_so(tmp_path, make_client):
    client = make_client(client_dist=tmp_path / "missing")

    response = client.get("/")

    assert response.status_code == 503
    assert "pnpm" in response.text
    assert client.get("/assets/app.js").status_code == 404


# ---- current branch ----------------------------------------------------------------


def test_current_branch_is_marked_in_cwd_repo(repo, make_client):
    git("checkout", "-b", "devon/feature", cwd=repo)
    git("checkout", "-b", "devon/other", cwd=repo)
    lineage.set_parent("devon/feature", "main", root=repo)
    lineage.set_parent("devon/other", "devon/feature", root=repo)

    got = branches(make_client(cwd_root=repo))

    assert {name: b["current"] for name, b in got.items()} == {
        "devon/feature": False,
        "devon/other": True,
    }


def test_detached_head_marks_nothing(repo, make_client):
    git("checkout", "-b", "devon/feature", cwd=repo)
    lineage.set_parent("devon/feature", "main", root=repo)
    git("checkout", "--detach", cwd=repo)

    got = branches(make_client(cwd_root=repo))

    assert not any(b["current"] for b in got.values())


class TestBranchLinks:
    def test_task_link_goes_through_the_tracker(self, tracker):
        link = task_link("abc-1", tracker=tracker)
        assert link is not None
        assert (link.label, link.href) == ("ABC-1", "https://tasks.example/abc-1")

    def test_task_without_tracker_is_plain_text(self):
        link = task_link("https://j.example/browse/ABC-1", tracker=None)
        assert link is not None
        assert link.href is None

    def test_tracker_ownership(self, tracker):
        assert owning_tracker("fake", tracker) is tracker
        assert owning_tracker(None, tracker) is tracker  # stored untagged
        assert owning_tracker("jira", tracker) is None
        assert owning_tracker("fake", None) is None

    def test_blank(self, tracker):
        assert task_link(None, tracker=tracker) is None
        assert task_link("", tracker=tracker) is None
        assert agent_link(None) is None

    def test_agent_link_keeps_custom_scheme(self):
        link = agent_link(AGENT_LINK)
        assert link is not None
        assert link.href == AGENT_LINK

    @pytest.mark.parametrize("value", ["javascript:alert(1)", "data:text/html,x"])
    def test_agent_link_never_links_executable_schemes(self, value):
        link = agent_link(value)
        assert link is not None
        assert link.href is None


def test_stack_heights_count_the_tallest_chain_of_each_stack():
    order = [("a", 0), ("b", 1), ("c", 2), ("d", 1), ("e", 0)]

    assert snapshot.stack_heights(order) == {"a": 3, "b": 3, "c": 3, "d": 3, "e": 1}


def test_tt_ui_serves_over_uvicorn(tmp_path):
    """Start the real `tt ui` on a free port, fetch the snapshot, stop it."""
    seed_repo(tmp_path, "api", {"devon/a": "main"})
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from toolbelt import main; main()",
            "ui",
            "--port",
            "0",
            "--no-build",
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        # Skip any import-time noise (e.g. matplotlib's font-cache notice).
        url = ""
        while not url.startswith("http://"):
            url = process.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:")
        response = httpx.get(url + "api/snapshot", timeout=10)
        assert response.status_code == 200
        assert [b["name"] for b in response.json()["branches"]] == ["devon/a"]
    finally:
        process.terminate()
        process.wait(timeout=10)
