"""Sync replays a child's own commits onto a parent that was rewritten, instead
of merging the parent's new commits into the old copies the child still holds.

Real git in tmp. Each parent rewrite below changes a file the child also has
(under the old commits), so merging would conflict add/add.
"""

import subprocess
from pathlib import Path

import pytest
import typer
from conftest import FakeForge, git

from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.sync import sync_stack


def _commit(worktree: Path, filename: str, content: str, message: str) -> None:
    (worktree / filename).write_text(content)
    git("add", "-A", cwd=worktree)
    git("commit", "-m", message, cwd=worktree)


def _two_branch_stack(repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """main <- devon/api (api.txt "api") <- devon/api_tests (tests.txt)."""
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    _commit(api_wt, "api.txt", "api\n", "api work")
    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    _commit(tests_wt, "tests.txt", "tests\n", "tests work")
    return api_wt, tests_wt


def _amend_api(api_wt: Path) -> None:
    """Rewrite the parent's one commit with other content for ``api.txt``."""
    (api_wt / "api.txt").write_text("api v2\n")
    git("commit", "-a", "--amend", "-m", "api work, amended", cwd=api_wt)


def _own_commits(branch: str, parent: str, repo: Path) -> list[str]:
    return git("log", "--format=%s", f"{parent}..{branch}", cwd=repo).splitlines()


def _merges(branch: str, parent: str, repo: Path) -> list[str]:
    return git(
        "log", "--merges", "--format=%H", f"{parent}..{branch}", cwd=repo
    ).split()


def test_a_child_is_rebased_onto_a_parent_that_was_amended(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _two_branch_stack(repo, tmp_path)
    _amend_api(api_wt)

    sync_stack(root=tests_wt, forge=FakeForge())

    assert _own_commits("devon/api_tests", "devon/api", repo) == ["tests work"]
    assert _merges("devon/api_tests", "devon/api", repo) == []
    assert (tests_wt / "api.txt").read_text() == "api v2\n"
    assert git("rev-parse", "devon/api_tests", cwd=repo) == git(
        "rev-parse", "origin/devon/api_tests", cwd=repo
    )


def test_a_grandchild_follows_a_parent_restacked_after_its_own_parent_landed(
    repo: Path, tmp_path: Path
):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    _commit(api_wt, "api.txt", "a\n", "api")
    mid_wt = tmp_path / "wt-mid"
    create_stacked_branch("mid", root=api_wt, wt_path=mid_wt)
    _commit(mid_wt, "api.txt", "a\nmid\n", "mid")
    leaf_wt = tmp_path / "wt-leaf"
    create_stacked_branch("leaf", root=mid_wt, wt_path=leaf_wt)
    _commit(leaf_wt, "api.txt", "a\nmid\nleaf\n", "leaf")
    # api lands as a squash: same content, new commit on main
    (repo / "api.txt").write_text("a\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "squash api", cwd=repo)
    git("push", "origin", "main", cwd=repo)

    sync_stack(root=leaf_wt, forge=FakeForge(merged={"devon/api"}))

    assert _own_commits("devon/mid", "main", repo) == ["mid"]
    assert _own_commits("devon/leaf", "devon/mid", repo) == ["leaf"]
    assert _merges("devon/leaf", "main", repo) == []
    assert (leaf_wt / "api.txt").read_text() == "a\nmid\nleaf\n"
    assert git("rev-parse", "devon/leaf", cwd=repo) == git(
        "rev-parse", "origin/devon/leaf", cwd=repo
    )


def test_a_parent_that_only_moved_forward_is_still_merged(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _two_branch_stack(repo, tmp_path)
    own = git("rev-parse", "devon/api_tests", cwd=repo)
    _commit(api_wt, "more.txt", "more\n", "api grows")

    sync_stack(root=tests_wt, forge=FakeForge())

    assert len(_merges("devon/api_tests", "devon/api", repo)) == 1
    assert git("rev-parse", "devon/api_tests^1", cwd=repo) == own
    assert (tests_wt / "more.txt").read_text() == "more\n"


def test_a_child_already_on_the_rewritten_parent_is_merged_not_replayed(
    repo: Path, tmp_path: Path
):
    api_wt, tests_wt = _two_branch_stack(repo, tmp_path)
    _amend_api(api_wt)
    sync_stack(root=tests_wt, forge=FakeForge())
    replayed = git("rev-parse", "devon/api_tests", cwd=repo)

    sync_stack(root=tests_wt, forge=FakeForge())

    assert git("rev-parse", "devon/api_tests", cwd=repo) == replayed


def test_a_real_overlap_stops_the_replay_and_resumes_after_it_is_resolved(
    repo: Path, tmp_path: Path
):
    api_wt, tests_wt = _two_branch_stack(repo, tmp_path)
    _commit(tests_wt, "api.txt", "api\ntests edit\n", "tests edits api")
    _amend_api(api_wt)

    with pytest.raises(typer.Exit):
        sync_stack(root=tests_wt, forge=FakeForge())

    (tests_wt / "api.txt").write_text("api v2\ntests edit\n")
    git("add", "api.txt", cwd=tests_wt)
    git("-c", "core.editor=true", "rebase", "--continue", cwd=tests_wt)

    sync_stack(root=tests_wt, forge=FakeForge())

    assert _own_commits("devon/api_tests", "devon/api", repo) == [
        "tests edits api",
        "tests work",
    ]
    assert git("rev-parse", "devon/api_tests", cwd=repo) == git(
        "rev-parse", "origin/devon/api_tests", cwd=repo
    )


def test_a_merge_the_user_left_unfinished_is_reported_not_replaced_by_a_replay(
    repo: Path, tmp_path: Path
):
    api_wt, tests_wt = _two_branch_stack(repo, tmp_path)
    _commit(tests_wt, "api.txt", "api\ntests edit\n", "tests edits api")
    _amend_api(api_wt)
    subprocess.run(
        ["git", "merge", "--no-edit", "devon/api"], check=False, cwd=tests_wt
    )

    with pytest.raises(typer.Exit):
        sync_stack(root=tests_wt, forge=FakeForge())

    assert git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=tests_wt)
