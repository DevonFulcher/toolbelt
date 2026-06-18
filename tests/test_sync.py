"""Integration tests for `sync` (merge path), real git in tmp."""

from pathlib import Path

from conftest import git

from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.sync import sync_stack


def _build_stack(repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """main <- devon/api <- devon/api_tests, each in its own worktree."""
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    return api_wt, tests_wt


def test_sync_propagates_parent_commit_down_the_stack(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)

    # New work on the stack root (devon/api)
    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)

    sync_stack(root=tests_wt)

    # devon/api's commit is now present in the leaf worktree
    assert (tests_wt / "api.txt").read_text() == "api change\n"


def test_sync_pulls_landed_base_changes(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)

    # Simulate work landing on main: commit on main and push to origin.
    (repo / "landed.txt").write_text("landed\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "landed on main", cwd=repo)
    git("push", "origin", "main", cwd=repo)

    sync_stack(root=tests_wt)

    # origin/main's change propagates through the whole stack
    assert (api_wt / "landed.txt").read_text() == "landed\n"
    assert (tests_wt / "landed.txt").read_text() == "landed\n"


def test_sync_is_idempotent(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)

    sync_stack(root=tests_wt)
    head_after_first = git("rev-parse", "HEAD", cwd=tests_wt)

    # Re-running changes nothing and does not error.
    sync_stack(root=tests_wt)
    assert git("rev-parse", "HEAD", cwd=tests_wt) == head_after_first
    assert git("status", "--porcelain", cwd=tests_wt) == ""


def test_sync_pushes_branches_to_origin(repo: Path, tmp_path: Path):
    _build_stack(repo, tmp_path)
    tests_wt = tmp_path / "wt-api-tests"

    sync_stack(root=tests_wt)

    remote_refs = git("ls-remote", "--heads", "origin", cwd=repo)
    assert "refs/heads/devon/api" in remote_refs
    assert "refs/heads/devon/api_tests" in remote_refs
