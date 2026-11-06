"""`sync --merge-base-branch` merges ``origin/<base>`` into the top of the stack.

Without it a clean change on the base stays out of the stack. Real git in tmp:
a throwaway repo and a bare remote.
"""

from pathlib import Path

import pytest
import typer
from conftest import FakeForge, git
from test_restack_by_merge import _chain, _land_on_main, _tip

from toolbelt.git.stack.sync import sync_stack


def test_a_clean_base_change_stays_out_without_the_option(repo: Path, tmp_path: Path):
    wts = _chain(repo, tmp_path)
    _land_on_main(repo, {"other.txt": "other\n"}, "someone else")

    sync_stack(root=wts["c"], forge=FakeForge())

    assert not (wts["a"] / "other.txt").exists()
    assert not (wts["c"] / "other.txt").exists()


def test_the_option_merges_the_base_into_the_top_and_down_the_stack(
    repo: Path, tmp_path: Path
):
    wts = _chain(repo, tmp_path)
    old = {n: _tip(f"devon/{n}", repo) for n in "abc"}
    _land_on_main(repo, {"other.txt": "other\n"}, "someone else")

    sync_stack(root=wts["c"], forge=FakeForge(), merge_base_branch=True)

    for name in "abc":
        branch = f"devon/{name}"
        assert (wts[name] / "other.txt").read_text() == "other\n"
        assert git("merge-base", "--is-ancestor", old[name], branch, cwd=repo) == ""
    assert git("merge-base", "devon/a", "main", cwd=repo) == _tip("main", repo)


def test_the_option_stops_on_a_conflict_and_resumes_when_rerun(
    repo: Path, tmp_path: Path
):
    wts = _chain(repo, tmp_path)
    _land_on_main(repo, {"a.txt": "different\n"}, "conflicts with a")

    with pytest.raises(typer.Exit):
        sync_stack(root=wts["c"], forge=FakeForge(), merge_base_branch=True)
    assert git("ls-files", "--unmerged", cwd=wts["a"]) != ""

    (wts["a"] / "a.txt").write_text("resolved\n")
    git("add", "a.txt", cwd=wts["a"])
    sync_stack(root=wts["c"], forge=FakeForge(), merge_base_branch=True)

    assert (wts["c"] / "a.txt").read_text() == "resolved\n"
    assert git("merge-base", "devon/a", "main", cwd=repo) == _tip("main", repo)
