"""Integration tests for `append` (create_stacked_branch), real git in tmp."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
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


def test_append_checkpoints_uncommitted_changes_onto_current_branch(
    repo: Path, tmp_path: Path
):
    (repo / "wip.txt").write_text("work in progress\n")

    wt_path = tmp_path / "wt-feature"
    create_stacked_branch("feature", root=repo, wt_path=wt_path)

    # the change is committed onto main (the branch it was made on) as a
    # checkpoint, so anything still running in `repo` keeps its exact
    # in-flight state — now committed instead of dirty.
    assert git("status", "--porcelain", cwd=repo) == ""
    assert (repo / "wip.txt").read_text() == "work in progress\n"
    # the new branch forks from that checkpoint, so it has the change too
    assert (wt_path / "wip.txt").read_text() == "work in progress\n"


def test_copy_dotfiles_copies_dotfiles_but_not_git_or_venv(repo: Path, tmp_path: Path):
    (repo / ".env").write_text("SECRET=1\n")
    (repo / ".config").mkdir()
    (repo / ".config" / "settings").write_text("x\n")
    # build artifact that must NOT be copied (rebuilt by uv sync instead)
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("stale\n")

    dest = tmp_path / "dest"
    dest.mkdir()
    copy_dotfiles(root=repo, wt_path=dest)

    assert (dest / ".env").read_text() == "SECRET=1\n"
    assert (dest / ".config" / "settings").read_text() == "x\n"
    # .git is managed by git per-worktree; .venv is excluded as a build artifact
    assert not (dest / ".git").exists()
    assert not (dest / ".venv").exists()


def test_copy_dotfiles_preserves_symlinks(repo: Path, tmp_path: Path):
    external = tmp_path / "shared-rules"
    external.mkdir()
    (external / "rule").write_text("shared\n")
    (repo / ".rules").symlink_to(external)

    dest = tmp_path / "dest"
    dest.mkdir()
    copy_dotfiles(root=repo, wt_path=dest)

    # the link is preserved (not dereferenced into an independent copy)
    assert (dest / ".rules").is_symlink()
    assert (dest / ".rules" / "rule").read_text() == "shared\n"


@pytest.mark.parametrize("target", ["settings", "missing"])
def test_copy_dotfiles_merges_existing_nested_symlinks(
    repo: Path, tmp_path: Path, target: str
) -> None:
    config = repo / ".config" / "nested"
    config.mkdir(parents=True)
    (config / "settings").write_text("source\n")
    (config / "rules").symlink_to(target)
    git("add", ".config", cwd=repo)
    git("commit", "-m", "Track config symlinks", cwd=repo)
    dest = tmp_path / "worktree"
    create_stacked_branch("config", root=repo, wt_path=dest)

    copy_dotfiles(root=repo, wt_path=dest)

    copied = dest / ".config" / "nested"
    assert (copied / "rules").is_symlink()
    assert (copied / "rules").readlink() == Path(target)
    assert (copied / "settings").read_text() == "source\n"
    (copied / "settings").write_text("worktree\n")
    assert (config / "settings").read_text() == "source\n"


@pytest.mark.parametrize("entry", [".env", ".config/settings", ".config"])
def test_copy_dotfiles_does_not_write_through_destination_symlinks(
    repo: Path, tmp_path: Path, entry: str
) -> None:
    (repo / ".env").write_text("source\n")
    (repo / ".config").mkdir()
    (repo / ".config" / "settings").write_text("source\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside"
    if entry == ".config":
        outside.mkdir()
        (outside / "settings").write_text("outside\n")
        protected = outside / "settings"
    else:
        outside.write_text("outside\n")
        protected = outside
    link = dest / entry
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    copy_dotfiles(root=repo, wt_path=dest)

    assert not link.is_symlink()
    assert (dest / ".env").read_text() == "source\n"
    assert (dest / ".config" / "settings").read_text() == "source\n"
    assert protected.read_text() == "outside\n"


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


def test_append_records_when_the_task_started_and_reparenting_keeps_it(
    repo: Path, tmp_path: Path
):
    before = datetime.now(UTC)
    create_stacked_branch("api", root=repo, wt_path=tmp_path / "wt-api")
    after = datetime.now(UTC)

    with lineage._repo_store(repo) as (s, repo_id):
        started = s.get_started_at(repo_id, "devon/api")
    assert started is not None
    assert before <= started <= after

    lineage.set_parent("devon/api", "develop", root=repo)  # reparent
    with lineage._repo_store(repo) as (s, repo_id):
        assert s.get_started_at(repo_id, "devon/api") == started


def _push_from_another_clone(
    git_remote: Path, tmp_path: Path, branch: str, filename: str
) -> str:
    """Commit ``filename`` on ``origin``'s ``branch`` from a second clone and
    return the new tip."""
    other = tmp_path / f"other-{branch.replace('/', '_')}"
    git("clone", str(git_remote), str(other), cwd=tmp_path)
    git("config", "user.email", "test@example.com", cwd=other)
    git("config", "user.name", "Test", cwd=other)
    git("checkout", branch, cwd=other)
    (other / filename).write_text("remote\n")
    git("add", "-A", cwd=other)
    git("commit", "-m", f"remote {filename}", cwd=other)
    git("push", "origin", branch, cwd=other)
    return git("rev-parse", "HEAD", cwd=other)


def test_append_from_base_behind_origin_forks_from_origin_tip(
    repo: Path, git_remote: Path, tmp_path: Path
):
    remote_tip = _push_from_another_clone(git_remote, tmp_path, "main", "new.txt")

    wt_path = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt_path)

    assert git("rev-parse", "devon/api", cwd=repo) == remote_tip
    assert git("rev-parse", "main", cwd=repo) == remote_tip
    assert (wt_path / "new.txt").read_text() == "remote\n"
    assert git("status", "--porcelain", cwd=repo) == ""


def test_append_from_base_up_to_date_forks_from_its_tip(repo: Path, tmp_path: Path):
    tip = git("rev-parse", "main", cwd=repo)

    create_stacked_branch("api", root=repo, wt_path=tmp_path / "wt-api")

    assert git("rev-parse", "devon/api", cwd=repo) == tip
    assert git("rev-parse", "main", cwd=repo) == tip


def test_append_from_base_with_checkpoint_behind_origin_merges_origin(
    repo: Path, git_remote: Path, tmp_path: Path
):
    remote_tip = _push_from_another_clone(git_remote, tmp_path, "main", "new.txt")
    (repo / "wip.txt").write_text("work in progress\n")

    wt_path = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt_path)

    assert git("status", "--porcelain", cwd=repo) == ""
    assert (wt_path / "new.txt").read_text() == "remote\n"
    assert (wt_path / "wip.txt").read_text() == "work in progress\n"
    git("merge-base", "--is-ancestor", remote_tip, "devon/api", cwd=repo)
    assert git("rev-parse", "devon/api", cwd=repo) == git("rev-parse", "main", cwd=repo)


def test_append_from_base_conflicting_with_origin_stops_and_leaves_base_clean(
    repo: Path, git_remote: Path, tmp_path: Path
):
    _push_from_another_clone(git_remote, tmp_path, "main", "README.md")
    (repo / "README.md").write_text("local\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "local readme", cwd=repo)
    before = git("rev-parse", "main", cwd=repo)

    with pytest.raises(typer.Exit):
        create_stacked_branch("api", root=repo, wt_path=tmp_path / "wt-api")

    assert git("rev-parse", "main", cwd=repo) == before
    assert git("status", "--porcelain", cwd=repo) == ""
    assert "devon/api" not in _branches(repo)


def test_append_from_stacked_branch_behind_its_remote_fast_forwards_it(
    repo: Path, git_remote: Path, tmp_path: Path
):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    git("push", "-u", "origin", "devon/api", cwd=api_wt)
    remote_tip = _push_from_another_clone(
        git_remote, tmp_path, "devon/api", "api-remote.txt"
    )

    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)

    assert git("rev-parse", "devon/api", cwd=repo) == remote_tip
    assert git("rev-parse", "devon/api_tests", cwd=repo) == remote_tip
    assert (tests_wt / "api-remote.txt").read_text() == "remote\n"
    assert lineage.get_parent("devon/api_tests", root=repo) == "devon/api"


def test_append_from_diverged_stacked_branch_stops_without_changes(
    repo: Path, git_remote: Path, tmp_path: Path
):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    git("push", "-u", "origin", "devon/api", cwd=api_wt)
    _push_from_another_clone(git_remote, tmp_path, "devon/api", "api-remote.txt")
    (api_wt / "api-local.txt").write_text("local\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "local api", cwd=api_wt)
    before = git("rev-parse", "devon/api", cwd=repo)

    with pytest.raises(typer.Exit):
        create_stacked_branch("api_tests", root=api_wt, wt_path=tmp_path / "wt-t")

    assert git("rev-parse", "devon/api", cwd=repo) == before
    assert "devon/api_tests" not in _branches(repo)
