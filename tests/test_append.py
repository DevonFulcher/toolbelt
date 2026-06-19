"""Integration tests for `append` (create_stacked_branch), real git in tmp."""

from pathlib import Path

from conftest import git

from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.worktrees import copy_dotfiles


def _branches(repo: Path) -> set[str]:
    out = git("branch", "--format=%(refname:short)", cwd=repo)
    return set(out.splitlines())


def test_append_creates_branch_worktree_and_lineage(repo: Path, tmp_path: Path):
    wt_path = tmp_path / "wt-api"
    new_branch = create_stacked_branch("api", root=repo, wt_path=wt_path)

    assert new_branch == "devon/api"
    assert "devon/api" in _branches(repo)
    assert lineage.get_parent("devon/api", root=repo) == "main"

    # worktree exists and has the new branch checked out
    assert wt_path.exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt_path) == "devon/api"

    # original worktree is restored to main
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"


def test_append_moves_uncommitted_changes(repo: Path, tmp_path: Path):
    (repo / "wip.txt").write_text("work in progress\n")

    wt_path = tmp_path / "wt-feature"
    create_stacked_branch("feature", root=repo, wt_path=wt_path)

    # the change is committed onto the new branch's worktree
    assert (wt_path / "wip.txt").read_text() == "work in progress\n"
    # main is clean again
    assert git("status", "--porcelain", cwd=repo) == ""
    assert not (repo / "wip.txt").exists()


def test_copy_dotfiles_copies_dotfiles_but_not_git(repo: Path, tmp_path: Path):
    (repo / ".env").write_text("SECRET=1\n")
    (repo / ".config").mkdir()
    (repo / ".config" / "settings").write_text("x\n")

    dest = tmp_path / "dest"
    dest.mkdir()
    copy_dotfiles(root=repo, wt_path=dest)

    assert (dest / ".env").read_text() == "SECRET=1\n"
    assert (dest / ".config" / "settings").read_text() == "x\n"
    # .git is managed by git per-worktree and must not be copied
    assert not (dest / ".git").exists()


def test_append_stacks_on_current_branch(repo: Path, tmp_path: Path):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)

    # append again from within the api worktree -> stacks on devon/api
    tests_wt = tmp_path / "wt-api-tests"
    new_branch = create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)

    assert new_branch == "devon/api_tests"
    assert lineage.get_parent("devon/api_tests", root=repo) == "devon/api"

    # the full stack resolves root -> leaf
    stack = lineage.resolve_stack("devon/api_tests", root=repo)
    assert stack == ["devon/api", "devon/api_tests"]
