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
