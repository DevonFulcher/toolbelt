"""The diff review: the files changed between revisions, their structural
diff, and language server answers, over real throwaway git repos."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolbelt.diff_review.difftastic import structural_diff
from toolbelt.diff_review.revisions import RevisionError, RevisionPair, Side
from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.ui.app import create_app

FAKE_SERVER = Path(__file__).parent / "fake_lsp_server.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, message: str, **files: str | None) -> None:
    """Writes the files (``None`` deletes one) and commits everything."""
    for name, text in files.items():
        file = repo / name
        if text is None:
            file.unlink()
        else:
            file.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    commit(
        path,
        "base",
        **{"keep.py": "x = 1\n", "gone.py": "y = 2\n", "old_name.py": "z = 3\n" * 5},
    )
    git(path, "switch", "-c", "feature")
    commit(
        path,
        "feature",
        **{"keep.py": "x = 2\n", "gone.py": None, "added.py": "w = 4\n"},
    )
    git(path, "mv", "old_name.py", "new_name.py")
    git(path, "commit", "-m", "rename")
    return path


def test_changed_files_lists_added_modified_deleted_and_renamed(repo: Path) -> None:
    files = RevisionPair(repo, "main", "feature").changed_files()

    assert {(f.status, f.path, f.old_path) for f in files} == {
        ("M", "keep.py", None),
        ("D", "gone.py", None),
        ("A", "added.py", None),
        ("R", "new_name.py", "old_name.py"),
    }


def test_the_old_side_is_where_head_forked_from_base(repo: Path) -> None:
    git(repo, "switch", "main")
    commit(repo, "main moves on", **{"keep.py": "x = 99\n"})
    pair = RevisionPair(repo, "main", "feature")

    assert pair.text(Side.OLD, "keep.py") == "x = 1\n"
    assert pair.text(Side.NEW, "keep.py") == "x = 2\n"


def test_a_revision_that_looks_like_an_option_is_refused(repo: Path) -> None:
    with pytest.raises(RevisionError):
        RevisionPair(repo, "--output=/tmp/x", "feature")


def test_an_unknown_path_is_a_revision_error(repo: Path) -> None:
    with pytest.raises(RevisionError):
        RevisionPair(repo, "main", "feature").text(Side.NEW, "nope.py")


@pytest.mark.skipif(shutil.which("difft") is None, reason="difftastic is not installed")
def test_structural_diff_reports_the_changed_tokens() -> None:
    diff = structural_diff("a.py", old_text="x = 1\n", new_text="x = 2\n")

    assert diff.language == "Python"
    assert diff.chunks is not None
    pair = diff.chunks[0][0]
    assert pair.lhs is not None and pair.rhs is not None
    assert [span.content for span in pair.lhs.changes] == ["1"]
    assert [span.content for span in pair.rhs.changes] == ["2"]


@pytest.mark.skipif(shutil.which("difft") is None, reason="difftastic is not installed")
def test_structural_diff_of_a_created_file_has_no_line_data() -> None:
    diff = structural_diff("a.py", old_text="", new_text="x = 1\n")

    assert diff.status == "created"
    assert diff.chunks is None


@pytest.fixture
def client(repo: Path) -> TestClient:
    store = StackStore(default_db_path())
    store.ensure_repo(
        RepoIdentity(git_common_dir=repo / ".git", path=repo, remote_url=None)
    )
    store.close()
    app = create_app(
        tracker=lambda: None, review_lsp_command=[sys.executable, str(FAKE_SERVER)]
    )
    return TestClient(app)


def repo_id() -> int:
    store = StackStore(default_db_path())
    try:
        return store.repos()[0].id
    finally:
        store.close()


def test_the_files_route_lists_the_changed_files(client: TestClient) -> None:
    response = client.get(
        "/api/review/files",
        params={"repo": repo_id(), "base": "main", "head": "feature"},
    )

    assert response.status_code == 200
    assert {f["path"] for f in response.json()} == {
        "keep.py",
        "gone.py",
        "added.py",
        "new_name.py",
    }


def test_the_files_route_says_why_a_revision_is_unknown(client: TestClient) -> None:
    response = client.get(
        "/api/review/files", params={"repo": repo_id(), "base": "main", "head": "nope"}
    )

    assert response.status_code == 400


def test_an_unknown_repo_is_a_404(client: TestClient) -> None:
    response = client.get(
        "/api/review/files", params={"repo": 999, "base": "main", "head": "feature"}
    )

    assert response.status_code == 404


@pytest.mark.skipif(shutil.which("difft") is None, reason="difftastic is not installed")
def test_the_file_route_returns_both_texts_and_the_structural_diff(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/review/file",
        params={
            "repo": repo_id(),
            "base": "main",
            "head": "feature",
            "status": "M",
            "path": "keep.py",
        },
    )

    body = response.json()
    assert (body["old_text"], body["new_text"]) == ("x = 1\n", "x = 2\n")
    assert body["difftastic"]["language"] == "Python"


def test_hover_comes_from_the_language_server(client: TestClient) -> None:
    response = client.get(
        "/api/review/hover",
        params={
            "repo": repo_id(),
            "base": "main",
            "head": "feature",
            "side": "new",
            "path": "keep.py",
            "line": 0,
            "character": 3,
        },
    )

    assert response.json() == {"text": "hover 0:3"}


def test_definition_is_relative_to_the_revisions_checkout(client: TestClient) -> None:
    response = client.get(
        "/api/review/definition",
        params={
            "repo": repo_id(),
            "base": "main",
            "head": "feature",
            "side": "old",
            "path": "keep.py",
            "line": 0,
            "character": 0,
        },
    )

    target = response.json()["target"]
    assert (target["path"], target["line"], target["character"]) == ("keep.py", 2, 4)


def test_a_path_outside_the_checkout_is_refused(client: TestClient) -> None:
    response = client.get(
        "/api/review/hover",
        params={
            "repo": repo_id(),
            "base": "main",
            "head": "feature",
            "side": "new",
            "path": "../../etc/passwd",
            "line": 0,
            "character": 0,
        },
    )

    assert response.status_code == 502
