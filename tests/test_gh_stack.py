"""Tests for mirroring lineage to GitHub stacked PRs (fake `StackedPrs`)."""

import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from conftest import FakeForge, git

from toolbelt.git.stack import gh_stack, lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.gh_stack import UNSUPPORTED_TTL, mirror_stack
from toolbelt.git.stack.sync import sync_stack

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeStackedPrs:
    """Every branch has a ready PR unless listed in `not_ready`."""

    def __init__(self, not_ready: Iterable[str] = (), available: bool = True) -> None:
        self.not_ready = set(not_ready)
        self.available = available
        self.links: list[list[str]] = []
        self.bases: list[str] = []
        self.readiness_checks = 0

    async def pr_is_ready(self, branch: str, *, cwd: Path) -> bool:
        self.readiness_checks += 1
        return branch not in self.not_ready

    def link(self, branches: list[str], *, base: str, cwd: Path) -> bool:
        self.bases.append(base)
        self.links.append(list(branches))
        return self.available


class Clock:
    def __init__(self) -> None:
        self.current = T0

    def __call__(self) -> datetime:
        return self.current


def _track(repo: Path, **parents: str) -> None:
    for branch, parent in parents.items():
        lineage.set_parent(branch, parent, root=repo)


def test_links_a_ready_chain_bottom_to_top(repo: Path):
    _track(repo, a="main", b="a", c="b")
    prs = FakeStackedPrs()

    mirror_stack(["a", "b", "c"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == [["a", "b", "c"]]


def test_links_with_the_stacks_trunk_as_base(repo: Path):
    _track(repo, a="release/1.0", b="a")
    prs = FakeStackedPrs()

    mirror_stack(["a", "b"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.bases == ["release/1.0"]


def test_unchanged_chain_makes_no_second_link_call(repo: Path):
    _track(repo, a="main", b="a")
    prs = FakeStackedPrs()

    mirror_stack(["a", "b"], root=repo, stacked_prs=prs, now=Clock())
    mirror_stack(["a", "b"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == [["a", "b"]]


def test_changed_membership_links_the_new_chain(repo: Path):
    _track(repo, a="main", b="a")
    prs = FakeStackedPrs()
    mirror_stack(["a", "b"], root=repo, stacked_prs=prs, now=Clock())

    _track(repo, c="b")
    mirror_stack(["a", "b", "c"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == [["a", "b"], ["a", "b", "c"]]


def test_reparent_links_the_new_order(repo: Path):
    _track(repo, a="main", b="a", c="b")
    prs = FakeStackedPrs()
    mirror_stack(["a", "b", "c"], root=repo, stacked_prs=prs, now=Clock())

    _track(repo, c="a", b="c")
    mirror_stack(["a", "b", "c"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links[-1] == ["a", "c", "b"]


def test_fewer_than_two_ready_branches_does_nothing(repo: Path):
    _track(repo, a="main", b="a")
    prs = FakeStackedPrs(not_ready=["b"])

    mirror_stack(["a", "b"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == []


def test_single_branch_stack_checks_nothing(repo: Path):
    _track(repo, a="main")
    prs = FakeStackedPrs()

    mirror_stack(["a"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == []
    assert prs.readiness_checks == 0


def test_chain_stops_at_first_branch_without_a_ready_pr(repo: Path):
    _track(repo, a="main", b="a", c="b", d="c")
    prs = FakeStackedPrs(not_ready=["c"])

    mirror_stack(["a", "b", "c", "d"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == [["a", "b"]]


def test_siblings_link_only_the_chain_with_the_longest_ready_run(repo: Path):
    # a -> b -> d and a -> c; leaf d wins by length, c is left unlinked.
    _track(repo, a="main", b="a", c="a", d="b")
    prs = FakeStackedPrs()

    mirror_stack(["a", "b", "d", "c"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == [["a", "b", "d"]]


def test_siblings_tie_goes_to_first_leaf_by_name(repo: Path):
    _track(repo, a="main", c="a", b="a")
    prs = FakeStackedPrs()

    mirror_stack(["a", "b", "c"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == [["a", "b"]]


def test_sibling_chain_wins_when_the_longer_one_is_not_ready(repo: Path):
    _track(repo, a="main", b="a", c="a", d="b")
    prs = FakeStackedPrs(not_ready=["b"])

    mirror_stack(["a", "b", "d", "c"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == [["a", "c"]]


def test_separate_trees_are_linked_independently(repo: Path):
    _track(repo, a="main", b="a", x="main", y="x")
    prs = FakeStackedPrs()

    mirror_stack(["a", "b", "x", "y"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == [["a", "b"], ["x", "y"]]


def test_unavailable_is_recorded_and_skipped_until_ttl_expires(repo: Path):
    _track(repo, a="main", b="a")
    clock = Clock()
    prs = FakeStackedPrs(available=False)

    mirror_stack(["a", "b"], root=repo, stacked_prs=prs, now=clock)
    assert lineage.get_gh_stack_unsupported_at(root=repo) == T0

    clock.current = T0 + UNSUPPORTED_TTL - timedelta(minutes=1)
    mirror_stack(["a", "b"], root=repo, stacked_prs=prs, now=clock)
    assert len(prs.links) == 1
    assert prs.readiness_checks == 2  # none on the skipped run

    clock.current = T0 + UNSUPPORTED_TTL
    prs.available = True
    mirror_stack(["a", "b"], root=repo, stacked_prs=prs, now=clock)
    assert len(prs.links) == 2
    assert lineage.get_gh_stack_unsupported_at(root=repo) is None
    assert lineage.get_published_chain("a", root=repo).branches == ["a", "b"]


def test_record_for_a_landed_bottom_is_pruned(repo: Path):
    _track(repo, a="main", b="a")
    prs = FakeStackedPrs()
    mirror_stack(["a", "b"], root=repo, stacked_prs=prs, now=Clock())

    # `a` lands: it is removed and `b` restacks onto main.
    lineage.remove_parent("a", root=repo)
    _track(repo, b="main", c="b")
    mirror_stack(["b", "c"], root=repo, stacked_prs=prs, now=Clock())

    assert prs.links == [["a", "b"], ["b", "c"]]
    assert lineage.get_published_chain("a", root=repo) is None


def test_link_failure_propagates_and_records_nothing(repo: Path):
    class Failing(FakeStackedPrs):
        def link(self, branches: list[str], *, base: str, cwd: Path) -> bool:
            raise typer.Exit(1)

    _track(repo, a="main", b="a")

    with pytest.raises(typer.Exit):
        mirror_stack(["a", "b"], root=repo, stacked_prs=Failing(), now=Clock())

    assert lineage.get_published_chain("a", root=repo) is None
    assert lineage.get_gh_stack_unsupported_at(root=repo) is None


def test_sync_mirrors_after_a_successful_sync(repo: Path, tmp_path: Path):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    prs = FakeStackedPrs()

    sync_stack(root=tests_wt, forge=FakeForge(), stacked_prs=prs)
    sync_stack(root=tests_wt, forge=FakeForge(), stacked_prs=prs)

    assert prs.links == [["devon/api", "devon/api_tests"]]


def test_sync_does_not_mirror_when_sync_fails(repo: Path, tmp_path: Path):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    # Conflicting edits to the same file on parent and child.
    (api_wt / "f.txt").write_text("parent\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "parent", cwd=api_wt)
    (tests_wt / "f.txt").write_text("child\n")
    git("add", "-A", cwd=tests_wt)
    git("commit", "-m", "child", cwd=tests_wt)
    prs = FakeStackedPrs()

    with pytest.raises(typer.Exit):
        sync_stack(root=tests_wt, forge=FakeForge(), stacked_prs=prs)

    assert prs.links == []
    assert prs.readiness_checks == 0


def _link_with(monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str) -> bool:
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(gh_stack.logged_process, "run", run)
    return gh_stack.GhStackedPrs().link(["a", "b"], base="main", cwd=Path.cwd())


def test_a_stack_that_still_holds_landed_prs_is_left_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
):
    refused = (
        "Cannot update stack: this would remove #1698, #1703 from the stack\n"
        "Include all existing PRs in the command to update the stack"
    )

    assert _link_with(monkeypatch, 1, refused) is True


def test_any_other_link_failure_still_stops(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(typer.Exit):
        _link_with(monkeypatch, 1, "HTTP 500")
