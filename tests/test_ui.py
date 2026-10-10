"""`tt ui`: the read-only web app, driven through FastAPI's TestClient.

The status fetcher and task tracker are injected via dependency overrides, so
no test touches `gh`, Jira or the real config. The last test starts the real CLI
command under uvicorn.
"""

import re
import subprocess
import sys
from html.parser import HTMLParser
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
from toolbelt.ui import app as ui_app
from toolbelt.ui.app import create_app
from toolbelt.ui.branch_links import agent_link, owning_tracker, task_link
from toolbelt.ui.status_cells import status_cells


class FakeStatuses:
    """A `StatusFetcher` serving seeded statuses (default: no PR)."""

    def __init__(self) -> None:
        self.statuses: dict[str, BranchStatus] = {}
        self.calls: list[tuple[str, Path]] = []

    async def __call__(self, branch: str, root: Path) -> BranchStatus:
        self.calls.append((branch, root))
        return self.statuses.get(branch, NO_PR)


class FakeTracker:
    """A `TaskTracker` serving seeded `TaskInfo`; unseeded refs fail to fetch."""

    name = "fake"

    def __init__(self) -> None:
        self.tasks: dict[str, TaskInfo] = {}
        self.fetches: list[str] = []

    def url_for(self, ref: str) -> str | None:
        return f"https://tasks.example/{ref}"

    def display_ref(self, ref: str) -> str:
        return ref.upper()

    def fetch(self, ref: str) -> TaskInfo | None:
        self.fetches.append(ref)
        return self.tasks.get(ref)


class Cells(HTMLParser):
    """The `<td>`s in a fragment: attributes and text, keyed by data-col."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.by_col: dict[str, dict[str, str]] = {}
        self.text: dict[str, str] = {}
        self.ids: list[str] = []
        self._raw = html
        self._col: str | None = None
        self.feed(html)

    def attrs_of(self, column: str) -> dict[str, str]:
        return self.by_col[column]

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            attributes = {k: v or "" for k, v in attrs}
            self._col = attributes["data-col"]
            self.by_col[self._col] = attributes
            self.text[self._col] = ""
            self.ids.append(attributes.get("id", ""))

    def handle_data(self, data):
        if self._col is not None:
            self.text[self._col] += data

    def handle_endtag(self, tag):
        if tag == "td":
            self._col = None


class Anchors(HTMLParser):
    """Maps each anchor's text to its (unescaped) href."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.hrefs: dict[str, str] = {}
        self._href: str | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_data(self, data):
        if self._href is not None and data.strip():
            self.hrefs[data.strip()] = self._href

    def handle_endtag(self, tag):
        if tag == "a":
            self._href = None


def seed_repo(
    root: Path,
    name: str,
    parents: dict[str, str],
    *,
    links: dict[str, tuple[str | None, str | None]] | None = None,
    task_tracker: str | None = "fake",
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
def make_client(fetcher: FakeStatuses, tracker: FakeTracker):
    def make(
        task_tracker: FakeTracker | None = None,
        cwd_root: Path | None = None,
        **app_kwargs,
    ) -> TestClient:
        app = create_app(cwd_root, **app_kwargs)
        app.dependency_overrides[ui_app.status_fetcher] = lambda: fetcher
        app.dependency_overrides[ui_app.task_tracker] = lambda: task_tracker
        return TestClient(app)

    return make


def status_url(repo_id: int) -> str:
    return f"/repos/{repo_id}/status"


def test_index_renders_tree_without_fetching_status(tmp_path, make_client, fetcher):
    repo_id = seed_repo(
        tmp_path,
        "api",
        {"devon/a": "main", "devon/b": "devon/a", "devon/c": "devon/a"},
    )

    response = make_client().get("/")

    assert response.status_code == 200
    html = response.text
    assert html.index("devon/a<") < html.index("devon/b<") < html.index("devon/c<")
    # One lazy placeholder per tracked branch; the untracked base has none.
    assert html.count('hx-trigger="load, refresh from:body') == 3
    assert f"{status_url(repo_id)}?branch=devon/b" in html
    assert fetcher.calls == []
    assert "/static/htmx.min.js" in html


def test_columns_are_separate_and_labelled(tmp_path, make_client):
    seed_repo(tmp_path, "api", {"devon/a": "main"})

    html = make_client().get("/").text

    headers = re.findall(r'<th data-col="(\w+)"[^>]*><button[^>]*>(\w+)', html)
    assert headers == [
        ("branch", "Branch"),
        ("pr", "PR"),
        ("ci", "CI"),
        ("review", "Review"),
        ("merge", "Merge"),
        ("task", "Task"),
        ("session", "Session"),
    ]
    # Every tracked row has one cell per column, in the same order.
    row = html[html.index('data-branch="devon/a"') :]
    cols = re.findall(r'<td[^>]*data-col="(\w+)"', row.split("</tr>")[0])
    assert cols == [key for key, _ in ui_app.COLUMNS]


def test_rows_carry_tree_data_for_the_sort_script(tmp_path, make_client):
    seed_repo(
        tmp_path,
        "api",
        {"devon/a": "main", "devon/b": "devon/a", "devon/c": "main"},
    )

    html = make_client().get("/").text

    rows = re.findall(
        r'<tr data-branch="([^"]*)" data-parent="([^"]*)"\s+'
        r'data-order="(\d+)" data-tracked="(\d)"\s+style="--depth: (\d+)"',
        html,
    )
    assert rows == [
        ("main", "", "0", "0", "0"),
        ("devon/a", "main", "1", "1", "1"),
        ("devon/b", "devon/a", "2", "1", "2"),
        ("devon/c", "main", "3", "1", "1"),
    ]


def test_filter_controls_are_rendered(tmp_path, make_client):
    seed_repo(tmp_path, "alpha", {"devon/one": "main"})
    seed_repo(tmp_path, "beta", {"devon/two": "main"})

    html = make_client().get("/").text

    assert 'name="q"' in html
    for column, values in {
        "pr": ["none", "draft", "open", "merged", "closed"],
        "ci": ["none", "running", "failed", "success"],
        "review": ["none", "requested", "approved", "reviewed"],
        "merge": ["conflicts", "clean", "none"],
    }.items():
        select = html.split(f'<select name="{column}">')[1].split("</select>")[0]
        options = re.findall(r'<option value="([^"]*)"', select)
        assert options == ["", *values]
    repo_select = html.split('<select name="repo">')[1].split("</select>")[0]
    assert ">alpha<" in repo_select and ">beta<" in repo_select
    assert 'data-view="tree"' in html and 'data-view="flat"' in html
    assert "/static/stack-table.js" in html


def test_static_sort_script_is_served(make_client):
    response = make_client().get("/static/stack-table.js")
    assert response.status_code == 200
    assert "layout" in response.text


def test_static_htmx_is_served(make_client):
    response = make_client().get("/static/htmx.min.js")
    assert response.status_code == 200
    assert "2.0.4" in response.text[:100_000]


def test_multi_repo_each_gets_a_section(tmp_path, make_client):
    seed_repo(tmp_path, "alpha", {"devon/one": "main"})
    seed_repo(tmp_path, "beta", {"devon/two": "develop"})

    html = make_client().get("/").text

    assert html.index("alpha") < html.index("devon/one")
    assert html.index("beta") < html.index("devon/two")
    assert "develop" in html


def test_no_repos(make_client):
    assert "No tracked stacks" in make_client().get("/").text


def test_missing_repo_path_is_flagged_and_not_fetched(tmp_path, make_client, fetcher):
    repo_id = seed_repo(tmp_path, "gone", {"devon/x": "main"}, exists=False)
    client = make_client()

    html = client.get("/").text
    assert "no longer exists" in html
    assert "repo path missing" in html
    assert 'hx-trigger="load' not in html

    partial = client.get(status_url(repo_id), params={"branch": "devon/x"})
    assert partial.status_code == 200
    assert "repo path missing" in partial.text
    assert fetcher.calls == []


def test_status_partial_shows_pr_ci_review_conflicts_comments(
    tmp_path, make_client, fetcher
):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.FAILED,
        review=ReviewState.APPROVED,
        url="https://github.com/o/r/pull/42",
        open_comments=2,
        has_conflicts=True,
    )

    response = make_client().get(status_url(repo_id), params={"branch": "devon/a"})

    html = response.text
    assert "#42 open" in html
    assert 'href="https://github.com/o/r/pull/42"' in html
    assert "2 open comments" in html
    assert fetcher.calls == [("devon/a", tmp_path / "api")]
    # A loaded cell must not re-fire "load", or it would refetch forever.
    assert 'hx-trigger="refresh from:body' in html
    assert 'hx-trigger="load' not in html

    # PR, CI, review and merge are separate cells carrying their own
    # filter value and sort key.
    cells = Cells(html)
    assert cells.by_col["pr"]["data-value"] == "open"
    assert cells.by_col["pr"]["data-sort"] == "42"
    assert cells.by_col["pr"]["data-search"] == "42"
    assert (cells.by_col["ci"]["data-value"], cells.by_col["ci"]["data-sort"]) == (
        "failed",
        "1",
    )
    assert cells.by_col["review"]["data-value"] == "approved"
    assert cells.by_col["merge"]["data-value"] == "conflicts"
    assert "CI" not in cells.text["pr"] and "failed" in cells.text["ci"]
    assert "approved" in cells.text["review"]
    assert "2 open comments" in cells.text["review"]
    assert "conflicts" in cells.text["merge"]
    # One request fills the whole row: the PR cell is the swap target, the
    # others are out-of-band swaps matched by id.
    assert "hx-swap-oob" not in cells.attrs_of("pr")
    for column in ("ci", "review", "merge"):
        assert cells.by_col[column]["hx-swap-oob"] == "true"
        assert cells.by_col[column]["id"].endswith(f"-{column}")


def test_index_placeholders_match_the_partial_cell_ids(tmp_path, make_client, fetcher):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    client = make_client()

    index_ids = Cells(client.get("/").text).ids
    partial_ids = Cells(
        client.get(status_url(repo_id), params={"branch": "devon/a"}).text
    ).ids

    for column in ("pr", "ci", "review", "merge"):
        assert [i for i in partial_ids if i.endswith(f"-{column}")]
        assert set(partial_ids) >= {i for i in index_ids if i.endswith(f"-{column}")}


def test_status_partial_draft_and_no_pr(tmp_path, make_client, fetcher):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "devon/a"})
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.DRAFT,
        ci=CiState.NONE,
        review=ReviewState.NONE,
        url="https://github.com/o/r/pull/7",
    )
    client = make_client()

    draft = client.get(status_url(repo_id), params={"branch": "devon/a"})
    none = client.get(status_url(repo_id), params={"branch": "devon/b"})

    assert "#7 draft" in draft.text
    assert Cells(draft.text).by_col["ci"]["data-value"] == "none"
    # A draft PR with no conflicts is mergeable.
    assert Cells(draft.text).by_col["merge"]["data-value"] == "clean"
    assert "no PR" in none.text
    no_pr = Cells(none.text)
    assert no_pr.by_col["pr"]["data-value"] == "none"
    assert no_pr.by_col["pr"]["data-sort"] == ""
    assert no_pr.by_col["merge"]["data-value"] == "none"


def test_status_unknown_repo_or_untracked_branch_is_404(tmp_path, make_client, fetcher):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    client = make_client()

    assert client.get(status_url(999), params={"branch": "devon/a"}).status_code == 404
    # The base is not tracked; nothing arbitrary reaches `gh`.
    for branch in ("main", "--web"):
        response = client.get(status_url(repo_id), params={"branch": branch})
        assert response.status_code == 404
    assert fetcher.calls == []


def test_mutating_methods_are_not_allowed(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    client = make_client()
    for url in ("/", f"{status_url(repo_id)}?branch=devon/a"):
        assert client.post(url).status_code == 405
        assert client.delete(url).status_code == 405


def test_link_rendering_in_index(tmp_path, make_client, tracker):
    repo_id = seed_repo(
        tmp_path,
        "api",
        {
            "devon/key": "main",
            "devon/deeplink": "main",
            "devon/blank": "main",
        },
        links={
            "devon/key": ("abc-1", None),
            "devon/deeplink": (None, "claude://claude.ai/local_session?id=a&b=c"),
        },
    )

    with_tracker = make_client(tracker).get("/").text
    anchors = Anchors(with_tracker)
    assert anchors.hrefs["ABC-1"] == "https://tasks.example/abc-1"
    # `&` is entity-escaped in the markup and parses back to the original;
    # the custom scheme is untouched.
    assert anchors.hrefs["Open session"] == "claude://claude.ai/local_session?id=a&b=c"
    # The title/status are fetched lazily, never while rendering the index.
    assert f"/repos/{repo_id}/task?branch=devon/key" in with_tracker
    assert tracker.fetches == []

    # No tracker configured: plain text, no fetch placeholder.
    without = make_client().get("/").text
    assert "abc-1" in without
    assert "abc-1" not in Anchors(without).hrefs
    assert "/task?branch" not in without


def test_task_of_another_tracker_is_plain_text(tmp_path, make_client, tracker):
    seed_repo(
        tmp_path,
        "api",
        {"devon/a": "main"},
        links={"devon/a": ("LIN-5", None)},
        task_tracker="linear",
    )

    html = make_client(tracker).get("/").text

    assert "LIN-5" in html
    assert "LIN-5" not in Anchors(html).hrefs
    assert "/task?branch" not in html


def test_task_partial_shows_title_and_status(tmp_path, make_client, tracker):
    repo_id = seed_repo(
        tmp_path, "api", {"devon/a": "main"}, links={"devon/a": ("abc-1", None)}
    )
    tracker.tasks["abc-1"] = TaskInfo(
        ref="ABC-1", title="Fix the <thing>", status="In Progress", url=None
    )

    response = make_client(tracker).get(
        f"/repos/{repo_id}/task", params={"branch": "devon/a"}
    )

    assert response.status_code == 200
    assert Anchors(response.text).hrefs["ABC-1"] == "https://tasks.example/abc-1"
    assert "Fix the &lt;thing&gt;" in response.text
    assert "In Progress" in response.text
    # A fetched cell refreshes on demand but never re-fires "load".
    assert 'hx-trigger="refresh from:body"' in response.text


def test_task_fetch_failure_shows_the_bare_ref(tmp_path, make_client, tracker):
    repo_id = seed_repo(
        tmp_path, "api", {"devon/a": "main"}, links={"devon/a": ("abc-1", None)}
    )

    response = make_client(tracker).get(
        f"/repos/{repo_id}/task", params={"branch": "devon/a"}
    )

    assert response.status_code == 200
    assert "ABC-1" in response.text
    assert "task-title" not in response.text


def test_task_fetches_are_cached_with_a_ttl(tmp_path, make_client, tracker):
    repo_id = seed_repo(
        tmp_path, "api", {"devon/a": "main"}, links={"devon/a": ("abc-1", None)}
    )
    tracker.tasks["abc-1"] = TaskInfo("ABC-1", "T", "Open", None)
    now = [100.0]
    client = make_client(tracker, task_ttl_seconds=60, clock=lambda: now[0])
    url, params = f"/repos/{repo_id}/task", {"branch": "devon/a"}

    client.get(url, params=params)
    now[0] += 59
    client.get(url, params=params)
    assert tracker.fetches == ["abc-1"]

    now[0] += 2
    client.get(url, params=params)
    assert tracker.fetches == ["abc-1", "abc-1"]


def test_task_partial_404s_without_a_stored_task(tmp_path, make_client, tracker):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    client = make_client(tracker)

    assert (
        client.get(f"/repos/{repo_id}/task", params={"branch": "devon/a"}).status_code
        == 404
    )
    assert (
        client.get("/repos/999/task", params={"branch": "devon/a"}).status_code == 404
    )
    assert tracker.fetches == []


def test_task_partial_without_tracker_never_fetches(tmp_path, make_client, tracker):
    repo_id = seed_repo(
        tmp_path, "api", {"devon/a": "main"}, links={"devon/a": ("abc-1", None)}
    )

    response = make_client().get(f"/repos/{repo_id}/task", params={"branch": "devon/a"})

    assert "abc-1" in response.text
    assert "hx-get" not in response.text
    assert tracker.fetches == []


def test_blank_links_render_dash(tmp_path, make_client):
    seed_repo(tmp_path, "api", {"devon/blank": "main"})
    html = make_client().get("/").text
    assert html.count("—") == 2


def test_current_branch_is_marked_in_cwd_repo(repo, make_client):
    git("checkout", "-b", "devon/feature", cwd=repo)
    git("checkout", "-b", "devon/other", cwd=repo)
    lineage.set_parent("devon/feature", "main", root=repo)
    lineage.set_parent("devon/other", "devon/feature", root=repo)

    html = make_client(cwd_root=repo).get("/").text

    assert html.count("branch current") == 1
    assert html.index("branch current") < html.index("devon/other<")
    assert html.index("devon/feature<") < html.index("branch current")


def test_detached_head_marks_nothing(repo, make_client):
    git("checkout", "-b", "devon/feature", cwd=repo)
    lineage.set_parent("devon/feature", "main", root=repo)
    git("checkout", "--detach", cwd=repo)

    html = make_client(cwd_root=repo).get("/").text

    assert "branch current" not in html


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
        link = agent_link("claude://x/y?z=1")
        assert link is not None
        assert link.href == "claude://x/y?z=1"

    @pytest.mark.parametrize("value", ["javascript:alert(1)", "data:text/html,x"])
    def test_agent_link_never_links_executable_schemes(self, value):
        link = agent_link(value)
        assert link is not None
        assert link.href is None


def test_tt_ui_serves_over_uvicorn(tmp_path):
    """Start the real `tt ui` on a free port, fetch the index, stop it."""
    seed_repo(tmp_path, "api", {"devon/a": "main"})
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from toolbelt import main; main()",
            "ui",
            "--port",
            "0",
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
        response = httpx.get(url, timeout=10)
        assert response.status_code == 200
        assert "devon/a" in response.text
    finally:
        process.terminate()
        process.wait(timeout=10)


class TestStatusCells:
    def test_unloaded_has_no_values(self):
        cells = status_cells(None)
        assert not cells.loaded
        assert (cells.pr, cells.ci, cells.review, cells.merge) == ("", "", "", "")

    def test_sort_keys_rank_urgent_states_first(self):
        def cells(ci=CiState.NONE, review=ReviewState.NONE):
            return status_cells(
                BranchStatus(
                    pr=PrState.OPEN,
                    ci=ci,
                    review=review,
                    url="https://github.com/o/r/pull/5",
                )
            )

        assert [cells(ci=c).ci_sort for c in (CiState.FAILED, CiState.RUNNING)] == [
            "1",
            "2",
        ]
        assert cells(ci=CiState.SUCCESS).ci_sort == "3"
        assert cells(ci=CiState.NONE).ci_sort == ""
        assert cells(review=ReviewState.REQUESTED).review_sort == "1"
        assert cells(review=ReviewState.APPROVED).review_sort == "3"
        assert cells().pr_sort == "5"

    def test_merge_is_only_meaningful_for_open_prs(self):
        def merge(pr, conflicts):
            return status_cells(
                BranchStatus(
                    pr=pr,
                    ci=CiState.NONE,
                    review=ReviewState.NONE,
                    url="https://github.com/o/r/pull/5",
                    has_conflicts=conflicts,
                )
            ).merge

        assert merge(PrState.OPEN, True) == "conflicts"
        assert merge(PrState.OPEN, False) == "clean"
        assert merge(PrState.MERGED, False) == "none"
        assert merge(PrState.NONE, False) == "none"
