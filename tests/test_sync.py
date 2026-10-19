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
    _api_wt, tests_wt = _build_stack(repo, tmp_path)
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


def test_sync_keeps_the_parent_an_ancestor_so_the_pr_diff_stays_clean(
    repo: Path, tmp_path: Path
):
    """GitHub diffs a PR from merge-base(base, head). Sync must leave the
    parent reachable from the branch, or every file the parent changed since
    the fork shows up as part of the branch's own diff."""
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)

    for i in range(2):
        (api_wt / f"api{i}.txt").write_text(f"{i}\n")
        git("add", "-A", cwd=api_wt)
        git("commit", "-m", f"api work {i}", cwd=api_wt)
        sync_stack(root=tests_wt, forge=FakeForge())

        assert git("merge-base", "devon/api", "HEAD", cwd=tests_wt) == git(
            "rev-parse", "devon/api", cwd=tests_wt
        )
        own_diff = git(
            "diff", "--name-only", "devon/api...HEAD", cwd=tests_wt
        ).splitlines()
        assert own_diff == ["own.txt"]


def test_sync_does_not_push_a_published_leaf_with_no_pr(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)
    git("push", "-u", "origin", "devon/api_tests", cwd=tests_wt)
    published = git("rev-parse", "origin/devon/api_tests", cwd=tests_wt)
    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)

    sync_stack(root=tests_wt, forge=FakeForge(no_pr=["devon/api_tests"]))

    # Merged locally, but not pushed again, since nothing has a PR to watch it.
    assert git("rev-parse", "origin/devon/api_tests", cwd=tests_wt) == published
    assert git("rev-parse", "HEAD", cwd=tests_wt) != published


def test_sync_does_not_repush_when_nothing_new_since_last_push(
    repo: Path, tmp_path: Path
):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)
    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)

    sync_stack(root=tests_wt, forge=FakeForge())
    pushed = git("rev-parse", "origin/devon/api_tests", cwd=tests_wt)

    sync_stack(root=tests_wt, forge=FakeForge())  # nothing new since the push

    assert git("rev-parse", "origin/devon/api_tests", cwd=tests_wt) == pushed
    assert git("rev-parse", "HEAD", cwd=tests_wt) == pushed


def test_sync_pushes_a_leaf_whose_pr_conflicts_so_the_resolution_reaches_github(
    repo: Path, tmp_path: Path
):
    """Absorbing the parent is not new work, but when the PR conflicts the
    merge is what fixes it, and only a push clears it on GitHub."""
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)
    sync_stack(root=tests_wt, forge=FakeForge())  # pushes the leaf once
    before = git("rev-parse", "origin/devon/api_tests", cwd=tests_wt)

    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)

    sync_stack(root=tests_wt, forge=FakeForge(conflicting=["devon/api_tests"]))
    assert git("rev-parse", "origin/devon/api_tests", cwd=tests_wt) == git(
        "rev-parse", "HEAD", cwd=tests_wt
    )
    assert git("rev-parse", "HEAD", cwd=tests_wt) != before


def test_sync_pushes_a_merge_only_leaf_when_the_parent_brought_changes(
    repo: Path, tmp_path: Path
):
    """The leaf's only new commit is the merge absorbing the parent, but its
    tree now differs from origin's, so the PR is stale until it is pushed."""
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)
    sync_stack(root=tests_wt, forge=FakeForge())  # pushes the leaf once
    before = git("rev-parse", "origin/devon/api_tests", cwd=tests_wt)

    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)
    sync_stack(root=tests_wt, forge=FakeForge())

    head = git("rev-parse", "HEAD", cwd=tests_wt)
    assert head != before
    assert git("rev-parse", "origin/devon/api_tests", cwd=tests_wt) == head


def test_sync_does_not_push_a_leaf_when_the_parent_had_nothing_new(
    repo: Path, tmp_path: Path
):
    _, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)
    sync_stack(root=tests_wt, forge=FakeForge())
    before = git("rev-parse", "origin/devon/api_tests", cwd=tests_wt)

    sync_stack(root=tests_wt, forge=FakeForge())  # parent unchanged: a no-op

    assert git("rev-parse", "origin/devon/api_tests", cwd=tests_wt) == before


def test_sync_pushes_once_new_work_and_a_pr_both_show_up(repo: Path, tmp_path: Path):
    """A quiet sync (no PR yet) of a published leaf accumulates merges locally
    with no push; once there's both a PR to watch it *and* new work of your own,
    the next sync pushes everything accumulated so far in one go."""
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)
    git("push", "-u", "origin", "devon/api_tests", cwd=tests_wt)
    published = git("rev-parse", "origin/devon/api_tests", cwd=tests_wt)

    for i in range(1):
        (api_wt / f"api{i}.txt").write_text(f"{i}\n")
        git("add", "-A", cwd=api_wt)
        git("commit", "-m", f"api work {i}", cwd=api_wt)
        sync_stack(root=tests_wt, forge=FakeForge(no_pr=["devon/api_tests"]))

    assert git("rev-parse", "origin/devon/api_tests", cwd=tests_wt) == published

    (tests_wt / "mine.txt").write_text("mine\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "my own new work", cwd=tests_wt)
    sync_stack(root=tests_wt, forge=FakeForge())  # PR now open, elsewhere

    remote_refs = git("ls-remote", "--heads", "origin", cwd=repo)
    assert "devon/api_tests" in remote_refs
    assert git("rev-parse", "origin/devon/api_tests", cwd=tests_wt) == git(
        "rev-parse", "HEAD", cwd=tests_wt
    )
    assert (tests_wt / "api0.txt").read_text() == "0\n"
    assert (tests_wt / "mine.txt").read_text() == "mine\n"


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


def test_merge_conflict_logs_the_merged_ref_and_files(
    repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    for i in range(12):
        (api_wt / f"f{i:02}.txt").write_text("from api\n")
        (tests_wt / f"f{i:02}.txt").write_text("from tests\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api files", cwd=api_wt)
    api_tip = git("rev-parse", "--short", "HEAD", cwd=api_wt)
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "tests files", cwd=tests_wt)

    with pytest.raises(typer.Exit):
        sync_stack(root=tests_wt, forge=FakeForge())

    assert f"Conflict merging 'devon/api' (tip {api_tip} \"api files\")." in caplog.text
    assert "Conflicted files: f00.txt, f01.txt," in caplog.text
    assert "f09.txt and 2 more." in caplog.text


def _publish_leaf_then_advance_locally(
    repo: Path, tmp_path: Path
) -> tuple[Path, str, str]:
    """devon/api_tests is on origin with one commit, then gets a newer local
    commit; returns its worktree, the old (origin) tip and the new local tip."""
    _, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)
    sync_stack(root=tests_wt, forge=FakeForge())  # first push, PR is open
    old_tip = git("rev-parse", "HEAD", cwd=tests_wt)
    (tests_wt / "newer.txt").write_text("newer\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "newer work", cwd=tests_wt)
    return tests_wt, old_tip, git("rev-parse", "HEAD", cwd=tests_wt)


def test_sync_fast_forwards_a_published_leaf_that_has_no_pr(repo: Path, tmp_path: Path):
    tests_wt, old_tip, new_tip = _publish_leaf_then_advance_locally(repo, tmp_path)
    assert git("rev-parse", "origin/devon/api_tests", cwd=tests_wt) == old_tip

    sync_stack(root=tests_wt, forge=FakeForge(no_pr=["devon/api_tests"]))

    assert git("rev-parse", "origin/devon/api_tests", cwd=tests_wt) == new_tip
    assert git("ls-remote", "origin", "devon/api_tests", cwd=repo).startswith(new_tip)


def test_sync_publishes_a_new_leaf_even_with_no_pr(repo: Path, tmp_path: Path):
    _, tests_wt = _build_stack(repo, tmp_path)
    (tests_wt / "own.txt").write_text("own work\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "own work", cwd=tests_wt)

    sync_stack(root=tests_wt, forge=FakeForge(no_pr=["devon/api_tests"]))

    assert "devon/api_tests" in git("ls-remote", "--heads", "origin", cwd=repo)


def test_sync_does_not_force_push_a_leaf_that_diverged_from_origin(
    repo: Path, tmp_path: Path
):
    tests_wt, _, _ = _publish_leaf_then_advance_locally(repo, tmp_path)
    # Someone else moves origin/devon/api_tests to a commit local lacks.
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)],
        check=True,
        capture_output=True,
    )
    git("config", "user.email", "o@example.com", cwd=other)
    git("config", "user.name", "Other", cwd=other)
    git("checkout", "devon/api_tests", cwd=other)
    (other / "theirs.txt").write_text("theirs\n")
    git("add", "-A", cwd=other)
    git("commit", "-m", "their work", cwd=other)
    git("push", "origin", "devon/api_tests", cwd=other)
    theirs = git("rev-parse", "HEAD", cwd=other)

    sync_stack(root=tests_wt, forge=FakeForge(no_pr=["devon/api_tests"]))

    # Local reconciled by merging origin in, but origin was left untouched.
    assert git("ls-remote", "origin", "devon/api_tests", cwd=repo).startswith(theirs)
    assert (tests_wt / "theirs.txt").exists()


def test_sync_does_not_push_a_published_leaf_that_is_up_to_date(
    repo: Path, tmp_path: Path
):
    tests_wt, _, _ = _publish_leaf_then_advance_locally(repo, tmp_path)
    git("push", "origin", "devon/api_tests", cwd=tests_wt)
    before = git("ls-remote", "origin", cwd=repo)

    sync_stack(root=tests_wt, forge=FakeForge(no_pr=["devon/api_tests"]))

    assert git("ls-remote", "origin", cwd=repo) == before
