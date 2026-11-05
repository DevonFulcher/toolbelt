"""The git checks that keep a stacked PR from being merged as anything but what
its parent's merge and the sync after it left."""

import subprocess
from pathlib import Path

import pytest

from toolbelt.ui.merge_queue_git import contains_commit, is_sync_merge


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def stack(tmp_path: Path):
    """main has the parent's squash commit; the child (built on the parent) has
    its own tip; ``origin/main`` and ``origin/parent`` are where they are."""
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    commit(repo, "base")
    git(repo, "checkout", "-b", "parent")
    parent_tip = commit(repo, "parent-change")
    git(repo, "checkout", "-b", "child")
    child_tip = commit(repo, "child-change")
    git(repo, "checkout", "main")
    squash = commit(repo, "squash-of-parent")
    git(repo, "update-ref", "refs/remotes/origin/main", squash)
    git(repo, "update-ref", "refs/remotes/origin/parent", parent_tip)
    git(repo, "checkout", "child")
    return repo, child_tip, squash, parent_tip


def merged_into_child(repo: Path, other: str) -> str:
    git(repo, "merge", "--no-ff", "-m", "sync", other)
    return git(repo, "rev-parse", "HEAD")


def test_a_merge_of_the_parents_squash_commit_is_what_a_sync_adds(stack):
    repo, child_tip, squash, _ = stack
    synced = merged_into_child(repo, squash)

    assert is_sync_merge(repo, child_tip, synced, refs=("parent", "main"))


def test_a_merge_of_the_parents_new_tip_is_what_a_sync_adds_to_a_grandchild(stack):
    repo, child_tip, _, _ = stack
    git(repo, "checkout", "parent")
    new_parent_tip = commit(repo, "parent-moved")
    git(repo, "update-ref", "refs/remotes/origin/parent", new_parent_tip)
    git(repo, "checkout", "child")
    synced = merged_into_child(repo, new_parent_tip)

    assert is_sync_merge(repo, child_tip, synced, refs=("parent", "main"))


def test_a_commit_pushed_on_top_is_not_a_sync(stack):
    repo, child_tip, _, _ = stack
    pushed = commit(repo, "someone-elses-change")

    assert not is_sync_merge(repo, child_tip, pushed, refs=("parent", "main"))


def test_a_merge_of_a_commit_on_no_branch_the_stack_builds_on_is_not_a_sync(stack):
    repo, child_tip, _, _ = stack
    git(repo, "checkout", "-b", "elsewhere", "main")
    stray = commit(repo, "stray")
    git(repo, "checkout", "child")
    merged = merged_into_child(repo, stray)

    assert not is_sync_merge(repo, child_tip, merged, refs=("parent", "main"))


def test_a_merge_that_does_not_start_from_the_queued_head_is_not_a_sync(stack):
    repo, child_tip, squash, _ = stack
    commit(repo, "extra")  # the child moved on after it was queued
    synced = merged_into_child(repo, squash)

    assert not is_sync_merge(repo, child_tip, synced, refs=("parent", "main"))


def test_a_commit_the_clone_does_not_have_is_not_a_sync(stack):
    repo, child_tip, _, _ = stack

    assert not is_sync_merge(repo, child_tip, "0" * 40, refs=("parent", "main"))


def test_a_head_contains_the_squash_commit_only_after_the_sync(stack):
    repo, child_tip, squash, _ = stack

    assert not contains_commit(repo, squash, child_tip)
    synced = merged_into_child(repo, squash)
    assert contains_commit(repo, squash, synced)
