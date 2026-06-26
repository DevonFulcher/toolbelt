"""Integration tests for restack-on-land (phase 4), real git in tmp."""

from pathlib import Path

import pytest
import typer

from conftest import FakeForge, git

from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.sync import sync_stack


def _branches(repo: Path) -> set[str]:
    return set(git("branch", "--format=%(refname:short)", cwd=repo).splitlines())


def _build_stack(repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """main <- devon/api (adds api.txt) <- devon/api_tests (adds tests.txt)."""
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    (api_wt / "api.txt").write_text("api\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)

    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    (tests_wt / "tests.txt").write_text("tests\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "tests work", cwd=tests_wt)
    return api_wt, tests_wt


def _squash_merge_to_main(repo: Path, filename: str, content: str) -> None:
    """Simulate a squash-merge: a new commit on main+origin (fresh SHA)."""
    (repo / filename).write_text(content)
    git("add", "-A", cwd=repo)
    git("commit", "-m", f"squash {filename}", cwd=repo)
    git("push", "origin", "main", cwd=repo)


def test_restack_on_clean_squash_land(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    _squash_merge_to_main(repo, "api.txt", "api\n")

    sync_stack(root=tests_wt, forge=FakeForge(merged={"devon/api"}))

    # api_tests re-parented to the base and the landed branch removed
    assert lineage.get_parent("devon/api_tests", root=repo) == "main"
    assert "devon/api" not in _branches(repo)

    # leaf has both the landed change (via main) and its own change
    assert (tests_wt / "api.txt").read_text() == "api\n"
    assert (tests_wt / "tests.txt").read_text() == "tests\n"

    # PR diff is clean: only api_tests's own file differs from main
    diff = git("diff", "--name-only", "main", "devon/api_tests", cwd=repo)
    assert diff.splitlines() == ["tests.txt"]


def test_abandoned_parent_is_not_restacked(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)

    # Nothing reported merged -> normal merge sync, no restack, no deletion.
    sync_stack(root=tests_wt, forge=FakeForge())

    assert "devon/api" in _branches(repo)
    assert lineage.get_parent("devon/api_tests", root=repo) == "devon/api"


def test_cascading_lands_in_one_pass(repo: Path, tmp_path: Path):
    # main <- api (api.txt) <- mid (mid.txt) <- leaf (leaf.txt)
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    (api_wt / "api.txt").write_text("api\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api", cwd=api_wt)

    mid_wt = tmp_path / "wt-mid"
    create_stacked_branch("mid", root=api_wt, wt_path=mid_wt)
    (mid_wt / "mid.txt").write_text("mid\n")
    git("add", "-A", cwd=mid_wt)
    git("commit", "-m", "mid", cwd=mid_wt)

    leaf_wt = tmp_path / "wt-leaf"
    create_stacked_branch("leaf", root=mid_wt, wt_path=leaf_wt)
    (leaf_wt / "leaf.txt").write_text("leaf\n")
    git("add", "-A", cwd=leaf_wt)
    git("commit", "-m", "leaf", cwd=leaf_wt)

    # Both api and mid land (squashed) onto main in the same window.
    _squash_merge_to_main(repo, "api.txt", "api\n")
    _squash_merge_to_main(repo, "mid.txt", "mid\n")

    sync_stack(root=leaf_wt, forge=FakeForge(merged={"devon/api", "devon/mid"}))

    # leaf collapses straight onto the surviving base; both landed branches gone
    assert lineage.get_parent("devon/leaf", root=repo) == "main"
    assert "devon/api" not in _branches(repo)
    assert "devon/mid" not in _branches(repo)
    # stale lineage keys for deleted branches are cleaned up
    assert lineage.all_parents(root=repo) == {"devon/leaf": "main"}

    # PR diff is clean: only leaf's own file differs from main
    diff = git("diff", "--name-only", "main", "devon/leaf", cwd=repo)
    assert diff.splitlines() == ["leaf.txt"]


def test_sync_on_untracked_branch_errors(repo: Path, tmp_path: Path):
    # main is not part of any tracked stack
    with pytest.raises(typer.Exit):
        sync_stack(root=repo, forge=FakeForge())


def test_cleanup_landed_branch_while_standing_in_it(repo: Path, tmp_path: Path):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    _squash_merge_to_main(repo, "api.txt", "api\n")

    # Run sync from the landed branch's own worktree.
    sync_stack(root=api_wt, forge=FakeForge(merged={"devon/api"}))

    # It is still cleaned up (cleanup runs from the main worktree).
    assert "devon/api" not in _branches(repo)
    assert lineage.get_parent("devon/api_tests", root=repo) == "main"


def test_restack_when_child_never_synced_with_parent(repo: Path, tmp_path: Path):
    # child branches off parent, then parent advances and lands WITHOUT the child
    # ever syncing -> restack must still replay only the child's own commits.
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    (api_wt / "api.txt").write_text("api1\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api1", cwd=api_wt)

    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    (tests_wt / "tests.txt").write_text("tests\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "tests", cwd=tests_wt)

    # api gains a second commit the child never merged.
    (api_wt / "api2.txt").write_text("api2\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api2", cwd=api_wt)

    # Land api (squash of both api commits) on main.
    (repo / "api.txt").write_text("api1\n")
    (repo / "api2.txt").write_text("api2\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "squash api", cwd=repo)
    git("push", "origin", "main", cwd=repo)

    sync_stack(root=tests_wt, forge=FakeForge(merged={"devon/api"}))

    assert lineage.get_parent("devon/api_tests", root=repo) == "main"
    assert "devon/api" not in _branches(repo)
    # Only the child's own file differs from main; both api files came via main.
    diff = git("diff", "--name-only", "main", "devon/api_tests", cwd=repo)
    assert diff.splitlines() == ["tests.txt"]


def test_restack_conflict_then_resume(repo: Path, tmp_path: Path):
    # api owns shared.txt; api_tests edits it -> its own commit touches shared.txt
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    (api_wt / "shared.txt").write_text("from api\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api shared", cwd=api_wt)

    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    (tests_wt / "shared.txt").write_text("from api + tests\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "tests shared", cwd=tests_wt)

    # Land api with DIFFERENT content on main -> rebase of api_tests conflicts.
    _squash_merge_to_main(repo, "shared.txt", "from main\n")

    forge = FakeForge(merged={"devon/api"})
    with pytest.raises(typer.Exit):
        sync_stack(root=tests_wt, forge=forge)

    # The rebase is left in progress for the user to resolve.
    assert (tests_wt / "shared.txt").read_text() != ""

    # Resolve the conflict and re-run -> rebase --continue path completes.
    (tests_wt / "shared.txt").write_text("resolved\n")
    git("add", "shared.txt", cwd=tests_wt)
    sync_stack(root=tests_wt, forge=forge)

    assert lineage.get_parent("devon/api_tests", root=repo) == "main"
    assert "devon/api" not in _branches(repo)
    assert (tests_wt / "shared.txt").read_text() == "resolved\n"
