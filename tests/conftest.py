"""Shared test fixtures.

Integration tests run real git against throwaway repos in ``tmp_path`` so they
exercise actual git behavior (merge bases, rebase --onto, squash artifacts).
"""

import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ambient git env vars so tests aren't hijacked by an outer repo.

    A git hook (e.g. pre-commit running pytest) exports GIT_DIR / GIT_INDEX_FILE
    / GIT_WORK_TREE pointing at the real repo. If those leak into the
    throwaway-repo subprocesses, git operates on the real repo instead and
    commands like `git clone` / `git worktree add` fail with exit 128.
    """
    for var in (
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
        "GIT_PREFIX",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    ):
        monkeypatch.delenv(var, raising=False)


class FakeForge:
    """Test `Forge`: reports a branch merged iff it was seeded as merged."""

    def __init__(self, merged: Iterable[str] = ()) -> None:
        self.merged = set(merged)

    async def pr_is_merged(self, branch: str) -> bool:
        return branch in self.merged


def git(*args: str, cwd: Path) -> str:
    """Run a git command in ``cwd`` and return stripped stdout."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def git_remote(tmp_path: Path) -> Path:
    """A bare repo standing in for ``origin``."""
    remote = tmp_path / "origin.git"
    remote.mkdir()
    git("init", "--bare", "-b", "main", cwd=remote)
    return remote


@pytest.fixture
def repo(tmp_path: Path, git_remote: Path) -> Path:
    """A clone of ``git_remote`` with an initial commit on ``main``."""
    path = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(git_remote), str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    git("config", "user.email", "test@example.com", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    # Non-interactive editor so merge/rebase --continue never block on $EDITOR.
    git("config", "core.editor", "true", cwd=path)
    git("checkout", "-b", "main", cwd=path)
    (path / "README.md").write_text("init\n")
    git("add", "-A", cwd=path)
    git("commit", "-m", "init", cwd=path)
    git("push", "-u", "origin", "main", cwd=path)
    return path


@pytest.fixture
def commit(repo: Path):
    """Helper to write a file and commit it on the current branch."""

    def _commit(filename: str, content: str, message: str | None = None) -> None:
        (repo / filename).write_text(content)
        git("add", "-A", cwd=repo)
        git("commit", "-m", message or f"add {filename}", cwd=repo)

    return _commit
