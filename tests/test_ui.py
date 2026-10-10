"""`tt ui`: the read-only web app, driven through FastAPI's TestClient.

The status fetcher and Jira base URL are injected via dependency overrides, so
no test touches `gh` or the real config. The last test starts the real CLI
command under uvicorn.
"""

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
from toolbelt.ui import app as ui_app
from toolbelt.ui.app import create_app
from toolbelt.ui.branch_links import agent_link, jira_base_url, jira_link


class FakeStatuses:
    """A `StatusFetcher` serving seeded statuses (default: no PR)."""

    def __init__(self) -> None:
        self.statuses: dict[str, BranchStatus] = {}
        self.calls: list[tuple[str, Path]] = []

    async def __call__(self, branch: str, root: Path) -> BranchStatus:
        self.calls.append((branch, root))
        return self.statuses.get(branch, NO_PR)


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
            jira, agent = (links or {}).get(branch, (None, None))
            store.set_parent(repo_id, branch, parent, jira=jira, agent_link=agent)
        return repo_id
    finally:
        store.close()


@pytest.fixture
def fetcher() -> FakeStatuses:
    return FakeStatuses()


@pytest.fixture
def make_client(fetcher: FakeStatuses):
    def make(base_url: str | None = None, cwd_root: Path | None = None) -> TestClient:
        app = create_app(cwd_root)
        app.dependency_overrides[ui_app.status_fetcher] = lambda: fetcher
        app.dependency_overrides[ui_app.jira_base_url] = lambda: base_url
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
    assert "CI failed" in html
    assert "review approved" in html
    assert "merge conflicts" in html
    assert "2 open comments" in html
    assert fetcher.calls == [("devon/a", tmp_path / "api")]
    # A loaded cell must not re-fire "load", or it would refetch forever.
    assert 'hx-trigger="refresh from:body' in html
    assert 'hx-trigger="load' not in html


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
    assert "CI " not in draft.text
    assert "no PR" in none.text


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


def test_link_rendering_in_index(tmp_path, make_client):
    seed_repo(
        tmp_path,
        "api",
        {
            "devon/key": "main",
            "devon/url": "main",
            "devon/deeplink": "main",
            "devon/blank": "main",
        },
        links={
            "devon/key": ("ABC-1", None),
            "devon/url": ("https://jira.example.com/browse/XYZ-9", None),
            "devon/deeplink": (None, "claude://claude.ai/local_session?id=a&b=c"),
        },
    )

    with_base = Anchors(make_client(base_url="https://jira.example.com/").get("/").text)
    assert with_base.hrefs["ABC-1"] == "https://jira.example.com/browse/ABC-1"
    assert with_base.hrefs["XYZ-9"] == "https://jira.example.com/browse/XYZ-9"
    # `&` is entity-escaped in the markup and parses back to the original;
    # the custom scheme is untouched.
    assert (
        with_base.hrefs["Open session"] == "claude://claude.ai/local_session?id=a&b=c"
    )

    without_base = make_client().get("/").text
    assert "ABC-1" in without_base
    assert "ABC-1" not in Anchors(without_base).hrefs


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
    def test_jira_key_with_and_without_base_url(self):
        link = jira_link("ABC-1", base_url="https://j.example/")
        assert link is not None
        assert link.href == "https://j.example/browse/ABC-1"
        bare = jira_link("ABC-1", base_url=None)
        assert bare is not None
        assert bare.href is None

    def test_jira_url_is_linked_as_is(self):
        link = jira_link("https://j.example/browse/ABC-1?x=1", base_url="https://o")
        assert link is not None
        assert link.href == "https://j.example/browse/ABC-1?x=1"

    def test_blank(self):
        assert jira_link(None, base_url=None) is None
        assert jira_link("", base_url=None) is None
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

    def test_jira_base_url_from_config(self, tmp_path):
        config = tmp_path / "config.yaml"
        assert jira_base_url(config) is None
        config.write_text("sync:\n  repos: []\n")
        assert jira_base_url(config) is None
        config.write_text("jira:\n  base_url: https://j.example\n")
        assert jira_base_url(config) == "https://j.example"


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
