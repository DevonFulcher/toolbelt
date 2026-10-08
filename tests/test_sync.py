"""Integration tests for `sync` (merge path), real git in tmp."""

import subprocess
from pathlib import Path

import pytest
import typer

from conftest import FakeForge, git

from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.ops import compress_branch
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

    sync_stack(root=tests_wt, forge=FakeForge())

    # devon/api's commit is now present in the leaf worktree
    assert (tests_wt / "api.txt").read_text() == "api change\n"


def test_sync_pulls_landed_base_changes(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)

    # Simulate work landing on main: commit on main and push to origin.
    (repo / "landed.txt").write_text("landed\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "landed on main", cwd=repo)
    git("push", "origin", "main", cwd=repo)

    sync_stack(root=tests_wt, forge=FakeForge())

    # origin/main's change propagates through the whole stack
    assert (api_wt / "landed.txt").read_text() == "landed\n"
    assert (tests_wt / "landed.txt").read_text() == "landed\n"


def test_sync_is_idempotent(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)

    sync_stack(root=tests_wt, forge=FakeForge())
    head_after_first = git("rev-parse", "HEAD", cwd=tests_wt)

    # Re-running changes nothing and does not error.
    sync_stack(root=tests_wt, forge=FakeForge())
    assert git("rev-parse", "HEAD", cwd=tests_wt) == head_after_first
    assert git("status", "--porcelain", cwd=tests_wt) == ""


def test_sync_pushes_branches_to_origin(repo: Path, tmp_path: Path):
    _build_stack(repo, tmp_path)
    tests_wt = tmp_path / "wt-api-tests"

    sync_stack(root=tests_wt, forge=FakeForge())

    remote_refs = git("ls-remote", "--heads", "origin", cwd=repo)
    assert "refs/heads/devon/api" in remote_refs
    assert "refs/heads/devon/api_tests" in remote_refs


def test_sync_reconciles_branch_with_its_own_remote(
    repo: Path, tmp_path: Path, git_remote: Path
):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    sync_stack(root=tests_wt, forge=FakeForge())  # establish branches on origin

    # Someone else advances origin/devon/api_tests via a separate clone.
    clone2 = tmp_path / "clone2"
    subprocess.run(
        ["git", "clone", str(git_remote), str(clone2)],
        check=True,
        capture_output=True,
    )
    git("config", "user.email", "other@example.com", cwd=clone2)
    git("config", "user.name", "Other", cwd=clone2)
    git("checkout", "devon/api_tests", cwd=clone2)
    (clone2 / "remote.txt").write_text("from remote\n")
    git("add", "-A", cwd=clone2)
    git("commit", "-m", "remote change", cwd=clone2)
    git("push", "origin", "devon/api_tests", cwd=clone2)

    # Diverge locally too, then sync: the remote change must be merged in and the
    # push must succeed (rather than being rejected non-fast-forward).
    (tests_wt / "local.txt").write_text("from local\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "local change", cwd=tests_wt)

    sync_stack(root=tests_wt, forge=FakeForge())

    assert (tests_wt / "remote.txt").read_text() == "from remote\n"
    assert (tests_wt / "local.txt").read_text() == "from local\n"


def test_sync_collapses_a_trailing_merge_commit(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)

    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)
    # api_tests has its own unique commit too, so merging api in is a real
    # (non-fast-forward) merge, not just a fast-forward.
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)

    sync_stack(root=tests_wt, forge=FakeForge())

    subjects = git("log", "--format=%s", cwd=tests_wt).splitlines()
    assert subjects.count("own work") == 1, "the branch's own commit stays distinct"
    assert subjects.count("Sync") == 1, "the merge collapses into one sync commit"
    assert (tests_wt / "api.txt").read_text() == "api change\n"
    assert (tests_wt / "own.txt").read_text() == "own work\n"


def test_sync_collapses_trailing_merges_across_multiple_syncs(
    repo: Path, tmp_path: Path
):
    """Sync noise never piles up beyond one commit, no matter how many times a
    branch is synced without adding any new work of its own — a later
    collapse sees past an earlier one (see _SYNC_COLLAPSE_TRAILER)."""
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)

    for i in range(3):
        (api_wt / f"api{i}.txt").write_text(f"{i}\n")
        git("add", "-A", cwd=api_wt)
        git("commit", "-m", f"api work {i}", cwd=api_wt)
        sync_stack(root=tests_wt, forge=FakeForge())

        subjects = git("log", "--format=%s", cwd=tests_wt).splitlines()
        assert subjects.count("own work") == 1
        assert (
            subjects.count("Sync") == 1
        ), f"sync noise accumulated past one commit after round {i}"
        for j in range(i + 1):
            assert (tests_wt / f"api{j}.txt").read_text() == f"{j}\n"


def test_sync_does_not_compress_a_branch_with_children(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)

    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)
    # Work landing on main makes syncing devon/api a real merge.
    (repo / "landed.txt").write_text("landed\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "landed on main", cwd=repo)
    git("push", "origin", "main", cwd=repo)

    sync_stack(root=tests_wt, forge=FakeForge())

    api_count = int(git("rev-list", "--count", "origin/main..HEAD", cwd=api_wt))
    assert api_count > 1, "devon/api has a child, so its merge must be kept"
    subjects = git("log", "--format=%s", cwd=tests_wt).splitlines()
    assert subjects.count("own work") == 1, "the leaf's own commit stays distinct"
    assert (tests_wt / "landed.txt").read_text() == "landed\n"
    assert (tests_wt / "own.txt").read_text() == "own work\n"


def test_sync_keeps_child_subject_after_parent_is_compressed(
    repo: Path, tmp_path: Path
):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    (api_wt / "api.txt").write_text("api\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "parent work", cwd=api_wt)
    # The child forks from the parent's commit, so that commit stays in the
    # child's history after the parent is squashed into a new one by hand.
    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    (tests_wt / "tests.txt").write_text("tests\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "child work", cwd=tests_wt)

    for i in range(2):
        (api_wt / f"api{i}.txt").write_text(f"{i}\n")
        git("add", "-A", cwd=api_wt)
        git("commit", "-m", f"parent follow-up {i}", cwd=api_wt)
        compress_branch(root=api_wt)

        sync_stack(root=tests_wt, forge=FakeForge())

        assert git("log", "-1", "--format=%s", cwd=api_wt) == "parent work"
        subjects = git("log", "--format=%s", cwd=tests_wt).splitlines()
        assert subjects.count("child work") == 1, (
            "the child's own commit must stay distinct, even once the "
            "parent it forked from has been rewritten away"
        )
        assert (tests_wt / f"api{i}.txt").read_text() == f"{i}\n"


def test_merge_conflict_then_resume(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)

    # api and api_tests change the same file divergently; api_tests hasn't synced.
    (api_wt / "shared.txt").write_text("from api\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api shared", cwd=api_wt)
    (tests_wt / "shared.txt").write_text("from tests\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "tests shared", cwd=tests_wt)

    with pytest.raises(typer.Exit):
        sync_stack(root=tests_wt, forge=FakeForge())

    # Resolve and re-run WITHOUT committing — sync concludes the merge itself.
    (tests_wt / "shared.txt").write_text("resolved\n")
    git("add", "shared.txt", cwd=tests_wt)
    sync_stack(root=tests_wt, forge=FakeForge())

    assert (tests_wt / "shared.txt").read_text() == "resolved\n"
    assert git("status", "--porcelain", cwd=tests_wt) == ""
