"""`tt ui`: the read-only web app, driven through FastAPI's TestClient.

The status fetcher is injected into `create_app` and the task tracker via a
dependency override, so no test touches `gh`, Jira or the real config. Polling
is driven a pass at a time (`refresh`), never by the background loop; the live
event stream is tested in test_ui_live.py. The last test starts the real CLI
command under uvicorn.
"""

import asyncio
import re
import subprocess
import sys
from datetime import UTC, datetime
import json
from html import unescape as html_unescape
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
from toolbelt.ui.pr_activity import PrFacts, TimelineItem
from toolbelt.ui.pr_fetch import PrSnapshot
from toolbelt.ui.branch_links import agent_link, owning_tracker, task_link
from toolbelt.ui.status_cells import status_cells


AGENT_LINK = "claude://claude.ai/epitaxy/local_0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


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
        app = create_app(cwd_root, fetch=fetcher, **app_kwargs)
        app.dependency_overrides[ui_app.task_tracker] = lambda: task_tracker
        return TestClient(app)

    return make


def refresh(client: TestClient) -> int:
    """Run one polling pass, as the background loop would."""
    return asyncio.run(client.app.state.refresher.run_once())  # type: ignore[attr-defined]


def test_index_renders_one_table_without_fetching_status(
    tmp_path, make_client, fetcher
):
    seed_repo(
        tmp_path,
        "api",
        {"devon/a": "main", "devon/b": "devon/a", "devon/c": "devon/a"},
    )

    response = make_client().get("/")

    assert response.status_code == 200
    html = response.text
    assert html.index("devon/a<") < html.index("devon/b<") < html.index("devon/c<")
    assert html.count("<table") == 1
    assert "<section" not in html  # one table, no per-repo sections
    # Rendering never waits on `gh`; the cells are placeholders until the
    # background refresher has an answer.
    assert fetcher.calls == []
    table = html.split("</table>")[0]
    # Each row's PR cell and its compact-mode status cell.
    assert table.count("loading…") == 6
    assert "hx-get" not in html
    assert "/static/htmx.min.js" in html
    assert "/static/live-updates.js" in html
    assert "Refresh" not in html
    assert 'id="live"' in html


def test_columns_are_separate_and_labelled(tmp_path, make_client):
    seed_repo(tmp_path, "api", {"devon/a": "main"})

    html = make_client().get("/").text

    headers = re.findall(r'<th data-col="(\w+)"[^>]*><button[^>]*>(\w+)', html)
    assert headers == [
        ("repo", "Repo"),
        ("branch", "Branch"),
        ("next", "Next"),
        ("pr", "PR"),
        ("ci", "CI"),
        ("review", "Review"),
        ("comments", "Comments"),
        ("merge", "Merge"),
        ("task", "Task"),
        ("session", "Session"),
    ]
    # Every row has one cell per column, in the same order, plus the combined
    # status cell the compact table shows in place of PR..Merge.
    row = html[html.index('data-branch="devon/a"') :]
    cols = re.findall(r'<td[^>]*data-col="(\w+)"', row.split("</tr>")[0])
    keys = [key for key, _ in ui_app.COLUMNS]
    assert cols == keys[: keys.index("task")] + ["status"] + keys[keys.index("task") :]


def test_rows_carry_tree_data_for_the_sort_script(tmp_path, make_client):
    repo_id = seed_repo(
        tmp_path,
        "api",
        {"devon/a": "main", "devon/b": "devon/a", "devon/c": "main"},
    )

    html = make_client().get("/").text

    rows = re.findall(
        r'<tr data-id="([^"]*)" data-repo="(\d+)" data-branch="([^"]*)"\s+'
        r'data-parent="([^"]*)" data-order="(\d+)"\s+'
        r'style="--depth: (\d+)"',
        html,
    )
    r = str(repo_id)
    # The trunk is not a row: a stack starts at its first branch, depth 0.
    assert rows == [
        (f"{r}:devon/a", r, "devon/a", "", "0", "0"),
        (f"{r}:devon/b", r, "devon/b", f"{r}:devon/a", "1", "1"),
        (f"{r}:devon/c", r, "devon/c", "", "2", "0"),
    ]
    assert 'data-branch="main"' not in html


def test_filter_controls_are_rendered(tmp_path, make_client):
    seed_repo(tmp_path, "alpha", {"devon/one": "main"})
    seed_repo(tmp_path, "beta", {"devon/two": "main"})

    html = make_client().get("/").text

    assert 'name="q"' in html
    for column, values in {
        "next": ["you", "ci", "others", "none"],
        "pr": ["none", "draft", "open", "merged", "closed"],
        "ci": ["none", "running", "failed", "success"],
        "review": ["none", "requested", "approved", "reviewed"],
        "comments": ["has comments", "none"],
        "merge": ["conflicts", "clean", "none"],
    }.items():
        select = html.split(f'<select name="{column}">')[1].split("</select>")[0]
        options = re.findall(r'<option value="([^"]*)"', select)
        assert options == ["", *values]
    # The Next filter reads in plain words while keeping the raw who value.
    next_select = html.split('<select name="next">')[1].split("</select>")[0]
    assert re.findall(r'<option value="([^"]*)">([^<]*)<', next_select) == [
        ("", "Any"),
        ("you", "Needs me"),
        ("ci", "Waiting on CI"),
        ("others", "Waiting on others"),
        ("none", "Done"),
    ]
    repo_select = html.split('name="repo"')[1].split("</select>")[0]
    assert ">alpha<" in repo_select and ">beta<" in repo_select
    assert 'data-view="tree"' in html and 'data-view="flat"' in html
    assert "/static/stack-table.js" in html


def test_static_scripts_are_served(make_client):
    client = make_client()
    table = client.get("/static/stack-table.js")
    assert table.status_code == 200
    assert "layout" in table.text
    live = client.get("/static/live-updates.js")
    assert live.status_code == 200
    assert "EventSource" in live.text


def test_static_htmx_is_served(make_client):
    response = make_client().get("/static/htmx.min.js")
    assert response.status_code == 200
    assert "2.0.4" in response.text[:100_000]


def test_multi_repo_is_one_table_grouped_by_repo_name(tmp_path, make_client):
    # Seeded out of name order: rows still group by repo, sorted by name.
    seed_repo(tmp_path, "beta", {"devon/two": "develop"})
    seed_repo(tmp_path, "alpha", {"devon/one": "main", "devon/uno": "devon/one"})

    html = make_client().get("/").text

    assert html.count("<table") == 1
    tbody = html.split("<tbody>")[1].split("</tbody>")[0]
    order = [
        m.group(1) for m in re.finditer(r'class="branch(?: [a-z ]+)?">([^<]+)<', tbody)
    ]
    assert order == ["devon/one", "devon/uno", "devon/two"]
    # The repo is a column of every row.
    repo_cells = re.findall(
        r'data-col="repo" data-value="\d+" data-sort="(\w+)"', tbody
    )
    assert repo_cells == ["alpha"] * 2 + ["beta"]


def test_same_branch_name_in_two_repos_has_distinct_row_ids(tmp_path, make_client):
    seed_repo(tmp_path, "alpha", {"devon/x": "main"})
    seed_repo(tmp_path, "beta", {"devon/x": "main"})

    html = make_client().get("/").text

    ids = re.findall(r'<tr data-id="([^"]*)"', html)
    assert len(ids) == len(set(ids)) == 2
    cell_ids = [i for i in Cells(html).ids if i]
    assert len(cell_ids) == len(set(cell_ids))


def test_no_repos(make_client):
    assert "No tracked stacks" in make_client().get("/").text


def test_missing_repo_path_is_flagged_and_never_polled(tmp_path, make_client, fetcher):
    seed_repo(tmp_path, "gone", {"devon/x": "main"}, exists=False)
    client = make_client()

    html = client.get("/").text
    assert "no longer exists" in html
    assert "repo path missing" in html

    assert refresh(client) == 0
    assert fetcher.calls == []


def test_status_cells_show_pr_ci_review_comments_conflicts(
    tmp_path, make_client, fetcher
):
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
    html = client.get("/").text

    assert fetcher.calls == [("devon/a", tmp_path / "api")]
    assert "#42 open" in html
    assert 'href="https://github.com/o/r/pull/42"' in html
    # PR, CI, review, comments and merge are separate cells carrying their own
    # filter value and sort key.
    row = html[html.index('data-branch="devon/a"') :].split("</tr>")[0]
    cells = Cells(row)
    assert cells.by_col["pr"]["data-value"] == "open"
    assert cells.by_col["pr"]["data-sort"] == "42"
    assert cells.by_col["pr"]["data-search"] == "42"
    assert (cells.by_col["ci"]["data-value"], cells.by_col["ci"]["data-sort"]) == (
        "failed",
        "1",
    )
    assert cells.by_col["review"]["data-value"] == "approved"
    assert (
        cells.by_col["comments"]["data-value"],
        cells.by_col["comments"]["data-sort"],
    ) == ("has comments", "2")
    assert cells.by_col["merge"]["data-value"] == "conflicts"
    assert "failed" in cells.text["ci"]
    assert "approved" in cells.text["review"]
    assert "comment" not in cells.text["review"]
    assert cells.text["comments"].strip() == "2"
    assert "conflicts" in cells.text["merge"]


def test_draft_and_no_pr_cells(tmp_path, make_client, fetcher):
    seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "devon/a"})
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.DRAFT,
        ci=CiState.NONE,
        review=ReviewState.NONE,
        url="https://github.com/o/r/pull/7",
    )
    client = make_client()
    refresh(client)

    html = client.get("/").text

    def row(branch: str) -> Cells:
        part = html[html.index(f'data-branch="{branch}"') :].split("</tr>")[0]
        return Cells(part)

    draft, none = row("devon/a"), row("devon/b")
    assert "#7 draft" in draft.text["pr"]
    assert draft.by_col["ci"]["data-value"] == "none"
    # A draft PR with no conflicts is mergeable.
    assert draft.by_col["merge"]["data-value"] == "clean"
    assert (
        draft.by_col["comments"]["data-value"],
        draft.by_col["comments"]["data-sort"],
    ) == ("none", "0")
    assert "no PR" in none.text["pr"]
    assert none.by_col["pr"]["data-value"] == "none"
    assert none.by_col["pr"]["data-sort"] == ""
    assert none.by_col["comments"]["data-sort"] == ""
    assert none.by_col["merge"]["data-value"] == "none"


def test_a_failed_lookup_is_shown_on_the_row(tmp_path, make_client, fetcher):
    seed_repo(tmp_path, "api", {"devon/a": "main"})
    fetcher.fail = True
    client = make_client()

    refresh(client)

    assert "status unavailable" in client.get("/").text


def test_task_endpoint_404s_for_untracked_branches(tmp_path, make_client, tracker):
    repo_id = seed_repo(
        tmp_path, "api", {"devon/a": "main"}, links={"devon/a": ("abc-1", None)}
    )
    client = make_client(tracker)

    # The base is not tracked; nothing arbitrary reaches the tracker.
    for branch in ("main", "--web"):
        response = client.get(f"/repos/{repo_id}/task", params={"branch": branch})
        assert response.status_code == 404
    assert tracker.fetches == []


def test_mutating_methods_are_not_allowed(tmp_path, make_client):
    repo_id = seed_repo(tmp_path, "api", {"devon/a": "main"})
    client = make_client()
    for url in ("/", "/events", f"/repos/{repo_id}/task?branch=devon/a"):
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
            "devon/deeplink": (None, AGENT_LINK),
        },
    )

    with_tracker = make_client(tracker).get("/").text
    anchors = Anchors(with_tracker)
    assert anchors.hrefs["ABC-1"] == "https://tasks.example/abc-1"
    # The custom scheme is untouched.
    assert anchors.hrefs["Open session"] == AGENT_LINK
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
    # A fetched cell re-polls but never re-fires "load".
    assert 'hx-trigger="every 60s"' in response.text


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


def test_task_fetch_failure_explains_why_in_the_tooltip(tmp_path, make_client, tracker):
    repo_id = seed_repo(
        tmp_path, "api", {"devon/a": "main"}, links={"devon/a": ("abc-1", None)}
    )
    tracker.reason = "JIRA_API_TOKEN not set"
    client = make_client(tracker)

    html = client.get(f"/repos/{repo_id}/task", params={"branch": "devon/a"}).text

    assert html.count('title="JIRA_API_TOKEN not set"') == 2  # cell and marker

    # A failure the tracker cannot explain still says something.
    tracker.reason = None
    html = client.get(f"/repos/{repo_id}/task", params={"branch": "devon/a"}).text
    assert "task details unavailable" in html


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

    table = html.split("</table>")[0]
    assert table.count("branch current") == 1
    assert table.index("branch current") < table.index("devon/other<")
    assert table.index("devon/feature<") < table.index("branch current")


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
        link = agent_link(AGENT_LINK)
        assert link is not None
        assert link.href == AGENT_LINK

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


def next_cell(html: str, branch: str) -> Cells:
    row = html[html.index(f'data-branch="{branch}"') :].split("</tr>")[0]
    return Cells(row)


def test_next_column_says_what_to_do_and_who_it_waits_on(
    tmp_path, make_client, fetcher
):
    seed_repo(
        tmp_path,
        "api",
        {"devon/none": "main", "devon/ci": "main", "devon/ready": "main"},
    )
    pull = "https://github.com/o/r/pull/"
    fetcher.statuses["devon/ci"] = BranchStatus(
        pr=PrState.OPEN, ci=CiState.RUNNING, review=ReviewState.NONE, url=pull + "1"
    )
    fetcher.statuses["devon/ready"] = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.APPROVED,
        url=pull + "2",
    )
    client = make_client()
    refresh(client)

    html = client.get("/").text

    none, ci, ready = (
        next_cell(html, b) for b in ("devon/none", "devon/ci", "devon/ready")
    )
    assert none.by_col["next"]["data-value"] == "you"
    assert none.text["next"].strip() == "Open PR"
    assert ci.by_col["next"]["data-value"] == "ci"
    assert ci.text["next"].strip() == "Waiting on CI"
    assert ready.text["next"].strip() == "Merge"
    # "needs me" sorts first: you, then ci, others, none.
    assert (
        none.by_col["next"]["data-sort"],
        ci.by_col["next"]["data-sort"],
    ) == ("1", "2")
    assert ready.by_col["next"]["data-search"] == "Merge"


def test_a_stacked_branch_is_blocked_until_its_parent_merges(
    tmp_path, make_client, fetcher
):
    seed_repo(tmp_path, "api", {"devon/a": "main", "devon/b": "devon/a"})
    pull = "https://github.com/o/r/pull/"
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.REQUESTED,
        url=pull + "1",
    )
    fetcher.statuses["devon/b"] = BranchStatus(
        pr=PrState.OPEN, ci=CiState.SUCCESS, review=ReviewState.APPROVED, url=pull + "2"
    )
    now = [1000.0]
    client = make_client(clock=lambda: now[0])
    refresh(client)

    html = client.get("/").text

    blocked = next_cell(html, "devon/b")
    assert blocked.text["next"].strip() == "Blocked on devon/a"
    assert blocked.by_col["next"]["data-value"] == "others"

    # Once the parent merges, the child can be merged.
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.MERGED,
        ci=CiState.SUCCESS,
        review=ReviewState.APPROVED,
        url=pull + "1",
    )
    now[0] += 100  # past the polling interval
    refresh(client)
    html = client.get("/").text
    assert next_cell(html, "devon/b").text["next"].strip() == "Merge"


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
    approved = BranchStatus(
        pr=PrState.OPEN, ci=CiState.SUCCESS, review=ReviewState.APPROVED, url=pull + "9"
    )
    # a needs me (CI failed); b and c are approved and stacked on it.
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.OPEN, ci=CiState.FAILED, review=ReviewState.NONE, url=pull + "1"
    )
    fetcher.statuses["devon/b"] = approved
    fetcher.statuses["devon/c"] = approved
    # x waits on a reviewer; y is approved on it.
    fetcher.statuses["devon/x"] = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.REQUESTED,
        url=pull + "2",
    )
    fetcher.statuses["devon/y"] = approved
    client = make_client()
    refresh(client)

    html = client.get("/").text

    for branch, parent, who in [
        ("devon/b", "devon/a", "you"),
        ("devon/c", "devon/b", "you"),
        ("devon/y", "devon/x", "others"),
    ]:
        cell = next_cell(html, branch)
        assert cell.text["next"].strip() == f"Blocked on {parent}"
        assert cell.by_col["next"]["data-value"] == who


def test_next_is_loading_until_the_status_is_in(tmp_path, make_client):
    seed_repo(tmp_path, "api", {"devon/a": "main"})

    cells = next_cell(make_client().get("/").text, "devon/a")

    assert cells.by_col["next"]["data-value"] == ""
    assert cells.by_col["next"]["data-sort"] == ""


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

    def test_comments_are_their_own_numeric_column(self):
        def cells(pr, comments):
            return status_cells(
                BranchStatus(
                    pr=pr,
                    ci=CiState.NONE,
                    review=ReviewState.NONE,
                    url="https://github.com/o/r/pull/5",
                    open_comments=comments,
                )
            )

        with_comments = cells(PrState.OPEN, 12)
        assert (with_comments.comments, with_comments.comments_sort) == (
            "has comments",
            "12",
        )
        none = cells(PrState.OPEN, 0)
        assert (none.comments, none.comments_sort) == ("none", "0")
        # No PR: nothing to order by.
        assert cells(PrState.NONE, 0).comments_sort == ""

    def test_failed_lookups_are_not_loading(self):
        cells = status_cells(None, failed=True)
        assert cells.failed and not cells.loaded
        assert not status_cells(None).failed


class TestPrDetailCells:
    NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)

    def facts(self, **overrides):
        base = dict(
            number=5,
            title="Add it",
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

    def cells(self, facts):
        status = BranchStatus(
            pr=PrState.DRAFT,
            ci=CiState.NONE,
            review=ReviewState.REQUESTED,
            url="https://github.com/o/r/pull/5",
        )
        return status_cells(status, facts=facts, now=self.NOW)

    def test_ages_failures_and_events_are_exposed_for_display(self):
        detail = self.cells(self.facts()).detail

        assert detail is not None
        assert detail.title == "Add it"
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
        assert [e["kind"] for e in detail.events] == ["opened", "review_requested"]
        assert detail.events[1] == {
            "t": "2026-10-06T06:00:00Z",
            "kind": "review_requested",
            "actor": "devon",
            "detail": "requested review from sam",
            "ci": "",
            "subject": "sam",
        }

    def test_a_finished_pr_has_a_fixed_age_that_no_longer_ticks(self):
        merged = self.facts(
            state="MERGED",
            is_draft=False,
            merged_at=datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
            requested_reviewers=(),
        )

        detail = self.cells(merged).detail

        assert detail is not None
        assert (detail.pr_age.text, detail.pr_age.since) == ("1d", None)
        assert detail.draft_age is None and detail.review_request_age is None

    def test_without_facts_there_is_no_detail(self):
        status = BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.NONE,
            review=ReviewState.NONE,
            url="https://github.com/o/r/pull/5",
        )

        assert status_cells(status).detail is None


SLOT_RE = re.compile(
    r'<div class="slot s-(\w+) v-(\w+)" data-slot="(\w+)">\s*'
    r'<span class="slot-label">(.*?)</span>\s*'
    r'<span class="slot-value[^"]*">(.*?)</span>\s*</div>',
    re.DOTALL,
)

#: Every labelled slot of a card, in the order the card lays them out.
CARD_SLOTS = {
    "branch": "Repo / branch",
    "next": "Next",
    "pr": "PR",
    "ci": "CI",
    "review": "Review",
    "comments": "Comments",
    "merge": "Merge",
    "task": "Task",
    "session": "Session",
    "parent": "Parent",
    "pr_age": "PR age",
    "in_draft": "In draft",
    "review_requested": "Review requested",
    "ci_failures": "CI failures",
    "reviewers": "Reviewers",
    "title": "PR title",
    "timeline": "Timeline",
}


def slots_of(card: str) -> dict[str, tuple[str, str, str]]:
    """``{key: (label, state, visible text)}`` for a card's slots."""
    found = {}
    for key, state, _, label, value in SLOT_RE.findall(card):
        text = re.sub(r"<[^>]+>", " ", value)
        found[key] = (label, state, " ".join(html_unescape(text).split()))
    return found


class TestViews:
    """Cards are rendered from the same rows as the table."""

    NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)

    def facts(self, **overrides) -> PrFacts:
        base = dict(
            number=7,
            title="Add <b>the</b> thing",
            author="devon",
            base_ref="main",
            head_oid="abc",
            state="OPEN",
            is_draft=True,
            created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
            merged_at=None,
            closed_at=None,
            requested_reviewers=(),
            commit_rollups=(("a1", "FAILURE"), ("b2", "FAILURE"), ("c3", "SUCCESS")),
            items=(
                TimelineItem(
                    datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
                    "commit",
                    "devon",
                    "a1a1a1a Fix it",
                    oid="a1",
                    ci="FAILURE",
                ),
            ),
        )
        base.update(overrides)
        return PrFacts(**base)

    def seed(self, tmp_path, fetcher):
        seed_repo(
            tmp_path,
            "api",
            {"devon/a": "main", "devon/b": "devon/a"},
            links={"devon/a": ("ABC-1", AGENT_LINK)},
        )
        fetcher.statuses["devon/a"] = BranchStatus(
            pr=PrState.DRAFT,
            ci=CiState.FAILED,
            review=ReviewState.NONE,
            url="https://github.com/o/r/pull/7",
            open_comments=0,
        )
        fetcher.facts["devon/a"] = self.facts()

    def client(self, make_client, tracker):
        return make_client(tracker, wall_clock=lambda: self.NOW)

    def card(self, html: str, branch: str) -> str:
        cards = html.split('<div class="cards"')[1]
        start = cards.index(f'data-branch="{branch}"')
        return cards[start:].split("</article>")[0]

    def test_there_is_one_card_per_tracked_branch_not_for_the_trunk(
        self, tmp_path, make_client, fetcher, tracker
    ):
        self.seed(tmp_path, fetcher)
        client = self.client(make_client, tracker)
        refresh(client)

        html = client.get("/").text

        cards = html.split('<div class="cards"')[1]
        assert re.findall(
            r'<article class="card" data-id="[^"]*" data-repo="\d+" data-branch="([^"]*)"',
            cards,
        ) == ["devon/a", "devon/b"]

    def test_a_populated_card_labels_every_value(
        self, tmp_path, make_client, fetcher, tracker
    ):
        self.seed(tmp_path, fetcher)
        fetcher.statuses["devon/b"] = BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.SUCCESS,
            review=ReviewState.REQUESTED,
            url="https://github.com/o/r/pull/8",
            open_comments=3,
            has_conflicts=True,
        )
        fetcher.facts["devon/b"] = self.facts(
            number=8,
            title="Second",
            is_draft=False,
            requested_reviewers=("sam", "lee"),
            items=(
                TimelineItem(
                    datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
                    "review_requested",
                    "devon",
                    "requested review from sam",
                    subject="sam",
                ),
            ),
        )
        client = self.client(make_client, tracker)
        refresh(client)

        html = client.get("/").text
        full = slots_of(self.card(html, "devon/b"))
        draft = slots_of(self.card(html, "devon/a"))

        assert {k: v[0] for k, v in full.items()} == CARD_SLOTS
        assert full["next"][2] == "Resolve conflicts needs you"
        assert full["pr"] == ("PR", "ok", "#8 open")
        assert full["ci"][2] == "success"
        assert full["review"][2] == "requested"
        assert full["comments"] == ("Comments", "ok", "3 comments")
        assert full["merge"][2] == "conflicts"
        assert full["pr_age"][2] == "5d"
        assert full["in_draft"] == ("In draft", "na", "not a draft")
        assert full["review_requested"] == ("Review requested", "ok", "1d")
        assert full["ci_failures"][2] == "2 failures"
        assert full["reviewers"] == ("Reviewers", "ok", "sam, lee")
        assert full["parent"] == ("Parent", "ok", "devon/a")
        assert full["title"][2] == "Second"
        assert "no task" in full["task"][2]
        # The draft one: task and session linked, in draft for 5 days.
        assert draft["task"] == ("Task", "ok", "ABC-1")
        assert draft["session"][1] == "ok"
        assert draft["in_draft"][2] == "5d"
        assert draft["parent"] == ("Parent", "na", "no parent (stack root)")
        # The PR title is escaped.
        assert "Add &lt;b&gt;the&lt;/b&gt; thing" in html

    def test_a_card_without_a_pr_still_has_every_slot(
        self, tmp_path, make_client, fetcher, tracker
    ):
        self.seed(tmp_path, fetcher)
        client = self.client(make_client, tracker)
        refresh(client)

        card = self.card(client.get("/").text, "devon/b")
        found = slots_of(card)

        assert {k: v[0] for k, v in found.items()} == CARD_SLOTS
        assert found["next"][2] == "Open PR needs you"
        assert found["pr"] == ("PR", "gap", "no PR")
        for key in (
            "ci",
            "review",
            "comments",
            "merge",
            "pr_age",
            "in_draft",
            "review_requested",
            "ci_failures",
            "reviewers",
            "title",
        ):
            assert found[key][1:] == ("na", "no PR"), key
        assert found["timeline"] == ("Timeline", "na", "no PR yet — no timeline")
        # Missing but settable reads as a gap, not as "not applicable".
        assert found["task"] == ("Task", "gap", "no task linked")
        assert found["session"] == ("Session", "gap", "no session linked")
        assert found["parent"] == ("Parent", "ok", "devon/a")
        assert "tl-mount" not in card

    def test_zero_and_empty_values_are_explicit(
        self, tmp_path, make_client, fetcher, tracker
    ):
        seed_repo(tmp_path, "api", {"devon/q": "main"})
        fetcher.statuses["devon/q"] = BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.NONE,
            review=ReviewState.NONE,
            url="https://github.com/o/r/pull/9",
        )
        fetcher.facts["devon/q"] = self.facts(
            number=9, is_draft=False, commit_rollups=(), items=()
        )
        client = self.client(make_client, tracker)
        refresh(client)

        found = slots_of(self.card(client.get("/").text, "devon/q"))

        assert {k: v[0] for k, v in found.items()} == CARD_SLOTS
        assert found["ci"] == ("CI", "na", "no CI configured")
        assert found["review"] == ("Review", "gap", "no review requested")
        assert found["comments"] == ("Comments", "na", "0 comments")
        assert found["merge"] == ("Merge", "ok", "clean")
        assert found["in_draft"] == ("In draft", "na", "not a draft")
        assert found["review_requested"] == (
            "Review requested",
            "na",
            "not requested",
        )
        assert found["ci_failures"] == ("CI failures", "na", "0 failures")
        assert found["reviewers"] == ("Reviewers", "na", "none requested")
        # A merged PR's merge slot says why it is empty.
        fetcher.statuses["devon/q"] = BranchStatus(
            pr=PrState.MERGED,
            ci=CiState.SUCCESS,
            review=ReviewState.APPROVED,
            url="https://github.com/o/r/pull/9",
        )
        now = [1000.0]
        client = make_client(tracker, wall_clock=lambda: self.NOW, clock=lambda: now[0])
        refresh(client)
        merged = slots_of(self.card(client.get("/").text, "devon/q"))
        assert merged["merge"] == ("Merge", "na", "n/a (PR merged)")

    def test_an_unloaded_card_has_every_slot_saying_loading(
        self, tmp_path, make_client, fetcher, tracker
    ):
        self.seed(tmp_path, fetcher)

        card = self.card(self.client(make_client, tracker).get("/").text, "devon/a")
        found = slots_of(card)

        assert {k: v[0] for k, v in found.items()} == CARD_SLOTS
        for key in CARD_SLOTS:
            if key in ("branch", "task", "session", "parent"):
                continue
            assert found[key][1:] == ("na", "loading…"), key

    def test_a_card_carries_the_data_its_timeline_track_is_drawn_from(
        self, tmp_path, make_client, fetcher, tracker
    ):
        self.seed(tmp_path, fetcher)
        client = self.client(make_client, tracker)
        refresh(client)

        card = self.card(client.get("/").text, "devon/a")

        match = re.search(r"data-track='([^']*)'", card)
        assert match is not None
        track = json.loads(html_unescape(match.group(1)))
        assert track["created"] == "2026-10-01T09:00:00Z"
        assert track["ended"] is None
        assert track["state"] == "draft"
        assert track["draft"] is True
        assert [(e["kind"], e["actor"], e["ci"]) for e in track["events"]] == [
            ("opened", "devon", ""),
            ("commit", "devon", "failed"),
        ]
        assert track["events"][1]["t"] == "2026-10-02T09:00:00Z"
        assert track["events"][1]["detail"] == "a1a1a1a Fix it"

    def test_a_finished_pr_track_ends_at_the_merge(
        self, tmp_path, make_client, fetcher, tracker
    ):
        seed_repo(tmp_path, "api", {"devon/m": "main"})
        fetcher.statuses["devon/m"] = BranchStatus(
            pr=PrState.MERGED,
            ci=CiState.SUCCESS,
            review=ReviewState.APPROVED,
            url="https://github.com/o/r/pull/3",
        )
        fetcher.facts["devon/m"] = self.facts(
            state="MERGED",
            is_draft=False,
            merged_at=datetime(2026, 10, 3, 9, 0, tzinfo=UTC),
        )
        client = self.client(make_client, tracker)
        refresh(client)

        card = self.card(client.get("/?done=1").text, "devon/m")

        match = re.search(r"data-track='([^']*)'", card)
        assert match is not None
        track = json.loads(html_unescape(match.group(1)))
        assert (track["state"], track["ended"]) == ("merged", "2026-10-03T09:00:00Z")

    def test_view_toggles_and_their_controls_are_rendered(self, tmp_path, make_client):
        seed_repo(tmp_path, "api", {"devon/a": "main"})

        html = make_client().get("/").text

        for view in ("tree", "flat", "cards"):
            assert f'data-view="{view}"' in html
        sort = html.split('<select name="sort">')[1].split("</select>")[0]
        assert "Needs my action first" in sort
        for control in ("done", "compact", "group"):
            assert f'id="{control}"' in html
        # The global timeline view and its controls are gone.
        assert 'data-view="timeline"' not in html
        assert 'name="kind"' not in html
        assert 'id="order"' not in html
        assert 'id="timeline' not in html
        assert "/static/pr-timeline.js" in html

    def test_the_compact_status_cell_combines_pr_ci_review_comments_merge(
        self, tmp_path, make_client, fetcher
    ):
        seed_repo(tmp_path, "api", {"devon/a": "main"})
        fetcher.statuses["devon/a"] = BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.FAILED,
            review=ReviewState.REQUESTED,
            url="https://github.com/o/r/pull/4",
            open_comments=2,
            has_conflicts=True,
        )
        client = make_client()
        refresh(client)

        cells = next_cell(client.get("/").text, "devon/a")

        status = cells.text["status"]
        for part in ("#4 open", "CI failed", "requested", "2 comments", "conflicts"):
            assert part in status

    def test_the_status_event_payload_includes_the_card(
        self, tmp_path, make_client, fetcher, tracker
    ):
        self.seed(tmp_path, fetcher)
        client = self.client(make_client, tracker)
        queue = client.app.state.hub.subscribe()  # type: ignore[attr-defined]

        refresh(client)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        statuses = [data for name, data in events if name == "status"]
        assert statuses
        assert all('class="card-live"' in data["card"] for data in statuses)
        assert any("Fix / re-run CI" in data["card"] for data in statuses)
        assert any("tl-mount" in data["card"] for data in statuses)
