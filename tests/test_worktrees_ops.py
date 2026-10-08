"""Unit tests for `delete_branch_and_worktree`'s async removal.

Deleting a worktree's files can be slow (tens of thousands of small files in
a venv/node_modules), so the directory is renamed away (instant) and its
contents deleted in a detached background process, rather than deleting it
inline before returning.
"""

import subprocess
import time
from pathlib import Path

import pytest

from conftest import git

from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.worktrees_ops import delete_branch_and_worktree


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_worktree_path_disappears_immediately(repo: Path, tmp_path: Path):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)

    delete_branch_and_worktree("devon/api", repo_root=repo)

    # Gone from its original path right away, even if the background delete
    # of the renamed-away copy hasn't finished yet.
    assert not api_wt.exists()


def test_background_delete_eventually_removes_the_trashed_copy(
    repo: Path, tmp_path: Path
):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)

    delete_branch_and_worktree("devon/api", repo_root=repo)

    # A tiny test worktree's background `rm -rf` may well finish before we
    # even get to check, so this only asserts it doesn't linger — not that
    # we catch it mid-flight.
    assert _wait_until(
        lambda: not list(tmp_path.glob(".toolbelt-trash-wt-api-*"))
    ), "background rm -rf never finished"


def test_refuses_to_delete_a_dirty_worktree_without_force(repo: Path, tmp_path: Path):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    (api_wt / "untracked.txt").write_text("oops\n")

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        delete_branch_and_worktree("devon/api", repo_root=repo)
    assert "modified or untracked" in excinfo.value.stderr

    # Left untouched — never renamed away, nothing lost.
    assert api_wt.exists()
    assert (api_wt / "untracked.txt").exists()


def test_force_deletes_a_dirty_worktree(repo: Path, tmp_path: Path):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    (api_wt / "untracked.txt").write_text("oops\n")

    delete_branch_and_worktree("devon/api", repo_root=repo, force=True)

    assert not api_wt.exists()
    assert (
        "devon/api"
        not in git("branch", "--format=%(refname:short)", cwd=repo).splitlines()
    )
