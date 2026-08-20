"""Integration tests for compress / diff-parent / set-parent cores."""

from pathlib import Path

import pytest
import typer

from conftest import git

from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.ops import (
    compress_branch,
    diff_parent_command,
    set_branch_parent,
)


def _commits_since(ref: str, *, cwd: Path) -> int:
    return int(git("rev-list", "--count", f"{ref}..HEAD", cwd=cwd))


# --- compress ---------------------------------------------------------------


def test_compress_squashes_branch_commits_into_one(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    for i in range(3):
        (wt / f"f{i}.txt").write_text(f"{i}\n")
        git("add", "-A", cwd=wt)
        git("commit", "-m", f"work {i}", cwd=wt)
    assert _commits_since("main", cwd=wt) == 3

    compress_branch(root=wt, message="squashed")

    assert _commits_since("main", cwd=wt) == 1
    assert git("log", "-1", "--format=%s", cwd=wt) == "squashed"
    # All the work is still present.
    for i in range(3):
        assert (wt / f"f{i}.txt").exists()


def test_compress_defaults_to_first_commit_subject(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    # Clean repo, so no "WIP" carry commit; these two are the branch's commits.
    for msg in ("first real", "second real"):
        (wt / f"{msg.replace(' ', '_')}.txt").write_text("x\n")
        git("add", "-A", cwd=wt)
        git("commit", "-m", msg, cwd=wt)

    compress_branch(root=wt, message=None)

    assert _commits_since("main", cwd=wt) == 1
    # Default message is the branch's first (oldest) commit subject.
    assert git("log", "-1", "--format=%s", cwd=wt) == "first real"


def test_compress_noop_on_single_commit(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    (wt / "only.txt").write_text("x\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "only commit", cwd=wt)
    assert _commits_since("main", cwd=wt) == 1
    before = git("rev-parse", "HEAD", cwd=wt)

    compress_branch(root=wt, message=None)  # single commit -> nothing to do

    assert git("rev-parse", "HEAD", cwd=wt) == before  # unchanged


def test_compress_errors_on_untracked_branch(repo: Path):
    git("checkout", "-b", "loose", cwd=repo)
    with pytest.raises(typer.Exit):
        compress_branch(root=repo, message="x")


# --- diff-parent ------------------------------------------------------------


def test_diff_parent_command_uses_lineage_parent(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)

    cmd = diff_parent_command(root=wt)

    assert cmd == ["git", "diff", "main...HEAD"]


def test_diff_parent_command_passes_extra_args(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)

    cmd = diff_parent_command(root=wt, extra_args=["--stat"])

    assert cmd == ["git", "diff", "main...HEAD", "--stat"]


# --- set-parent -------------------------------------------------------------


def test_set_parent_repoints_lineage(repo: Path, tmp_path: Path):
    api = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api)
    tests = tmp_path / "wt-tests"
    create_stacked_branch("api_tests", root=api, wt_path=tests)
    # api_tests -> api -> main; repoint api_tests straight onto main.
    assert lineage.get_parent("devon/api_tests", root=tests) == "devon/api"

    set_branch_parent(root=tests, new_parent="main")

    assert lineage.get_parent("devon/api_tests", root=tests) == "main"


def test_set_parent_rejects_cycle(repo: Path, tmp_path: Path):
    api = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api)
    tests = tmp_path / "wt-tests"
    create_stacked_branch("api_tests", root=api, wt_path=tests)
    # Standing on devon/api, try to set its parent to its own descendant.
    with pytest.raises(typer.Exit):
        set_branch_parent(root=api, new_parent="devon/api_tests")


def test_set_parent_rejects_missing_branch(repo: Path, tmp_path: Path):
    api = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api)
    with pytest.raises(typer.Exit):
        set_branch_parent(root=api, new_parent="does/not/exist")
