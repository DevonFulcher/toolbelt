"""When a parent lands, `sync` merges the base into each child over snapshots
instead of replaying the child's commits: no history rewrite, no force-push.

Real git in tmp: a throwaway repo and a bare remote.
"""

from pathlib import Path

import pytest
import typer
from conftest import FakeForge, git

from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.sync import sync_stack


def _commit(worktree: Path, filename: str, content: str, message: str) -> None:
    (worktree / filename).write_text(content)
    git("add", "-A", cwd=worktree)
    git("commit", "-m", message, cwd=worktree)


def _stacked(
    name: str, *, parent_wt: Path, tmp_path: Path, repo: Path, files: dict[str, str]
) -> Path:
    """A pushed branch ``devon/<name>`` off ``parent_wt``'s branch, adding ``files``."""
    wt = tmp_path / f"wt-{name}"
    create_stacked_branch(name, root=parent_wt, wt_path=wt)
    for filename, content in files.items():
        (wt / filename).write_text(content)
    git("add", "-A", cwd=wt)
    git("commit", "-m", f"{name} work", cwd=wt)
    git("push", "-u", "origin", f"devon/{name}", cwd=wt)
    return wt


def _chain(repo: Path, tmp_path: Path) -> dict[str, Path]:
    """main <- devon/a (a.txt) <- devon/b (b.txt) <- devon/c (c.txt), all pushed."""
    a = _stacked(
        "a", parent_wt=repo, tmp_path=tmp_path, repo=repo, files={"a.txt": "a\n"}
    )
    b = _stacked("b", parent_wt=a, tmp_path=tmp_path, repo=repo, files={"b.txt": "b\n"})
    c = _stacked("c", parent_wt=b, tmp_path=tmp_path, repo=repo, files={"c.txt": "c\n"})
    return {"a": a, "b": b, "c": c}


def _land_on_main(repo: Path, files: dict[str, str], message: str) -> None:
    for filename, content in files.items():
        (repo / filename).write_text(content)
    git("add", "-A", cwd=repo)
    git("commit", "-m", message, cwd=repo)
    git("push", "origin", "main", cwd=repo)


def _tip(ref: str, repo: Path) -> str:
    return git("rev-parse", ref, cwd=repo)


def _tree(ref: str, repo: Path) -> str:
    return git("rev-parse", f"{ref}^{{tree}}", cwd=repo)


def _only_fast_forwarded(before: str, branch: str, repo: Path) -> bool:
    """origin's ``branch`` is now a descendant of ``before`` (nothing was forced)."""
    git("fetch", "origin", cwd=repo)
    return (
        git("merge-base", "--is-ancestor", before, f"origin/{branch}", cwd=repo) == ""
    )


def _merge_count(old_tip: str, branch: str, repo: Path) -> int:
    return int(
        git(
            "rev-list",
            "--merges",
            "--first-parent",
            "--count",
            f"{old_tip}..{branch}",
            cwd=repo,
        )
    )


def test_a_squash_landed_parent_gives_each_descendant_one_merge_and_no_force_push(
    repo: Path, tmp_path: Path
):
    wts = _chain(repo, tmp_path)
    old = {n: _tip(f"devon/{n}", repo) for n in "bc"}
    old_trees = {n: _tree(f"devon/{n}", repo) for n in "bc"}
    _land_on_main(repo, {"a.txt": "a\n"}, "squash a")

    sync_stack(root=wts["c"], forge=FakeForge(merged={"devon/a"}))

    assert lineage.get_parent("devon/b", root=repo) == "main"
    assert lineage.get_parent("devon/c", root=repo) == "devon/b"
    for name in "bc":
        branch = f"devon/{name}"
        assert _merge_count(old[name], branch, repo) == 1
        assert _tree(branch, repo) == old_trees[name]
        assert _only_fast_forwarded(old[name], branch, repo)
    # b is pushed (c is based on it); c's tree is unchanged, so sync leaves its
    # PR alone and its remote stays where it was.
    assert _tip("devon/b", repo) == _tip("origin/devon/b", repo)
    assert _tip("origin/devon/c", repo) == old["c"]
    assert git("merge-base", "devon/b", "main", cwd=repo) == _tip("main", repo)
    assert git("diff", "--name-only", "main", "devon/b", cwd=repo) == "b.txt"
    assert git("diff", "--name-only", "devon/b", "devon/c", cwd=repo) == "c.txt"
    assert "devon/a" not in git("branch", "--format=%(refname:short)", cwd=repo)
    assert not wts["a"].exists()


def test_other_work_that_landed_on_main_is_merged_into_the_children(
    repo: Path, tmp_path: Path
):
    wts = _chain(repo, tmp_path)
    old = {n: _tip(f"devon/{n}", repo) for n in "bc"}
    _land_on_main(repo, {"a.txt": "a\n"}, "squash a")
    _land_on_main(repo, {"other.txt": "other\n"}, "someone else")

    sync_stack(root=wts["c"], forge=FakeForge(merged={"devon/a"}))

    for name in "bc":
        assert (wts[name] / "other.txt").read_text() == "other\n"
        assert (wts[name] / "a.txt").read_text() == "a\n"
        assert _only_fast_forwarded(old[name], f"devon/{name}", repo)
    assert git("diff", "--name-only", "main", "devon/b", cwd=repo) == "b.txt"


def test_a_child_main_overlaps_falls_back_to_the_replay_and_siblings_are_unaffected(
    repo: Path, tmp_path: Path
):
    a = _stacked(
        "a", parent_wt=repo, tmp_path=tmp_path, repo=repo, files={"a.txt": "a\n"}
    )
    # Sorted before and after the conflicting child, so either order is covered.
    ok1 = _stacked(
        "b1", parent_wt=a, tmp_path=tmp_path, repo=repo, files={"b1.txt": "1\n"}
    )
    bad = _stacked(
        "b2", parent_wt=a, tmp_path=tmp_path, repo=repo, files={"shared.txt": "child\n"}
    )
    ok2 = _stacked(
        "b3", parent_wt=a, tmp_path=tmp_path, repo=repo, files={"b3.txt": "3\n"}
    )
    old = {n: _tip(f"devon/{n}", repo) for n in ("b1", "b2", "b3")}
    _land_on_main(
        repo, {"a.txt": "a\n", "shared.txt": "main\n"}, "squash a, edits shared"
    )

    forge = FakeForge(merged={"devon/a"})
    with pytest.raises(typer.Exit):
        sync_stack(root=bad, forge=forge)

    # The overlapping child is left mid-rebase for the user, as before.
    assert (bad / ".git").exists()
    assert git("status", "--porcelain=v1", cwd=bad).count("shared.txt") == 1
    assert _tip("origin/devon/b2", repo) == old["b2"]

    # The clean ones were merged, whichever order they came in.
    for name, wt in (("b1", ok1), ("b3", ok2)):
        branch = f"devon/{name}"
        if _tip(branch, repo) != old[name]:
            assert _merge_count(old[name], branch, repo) == 1
            assert _only_fast_forwarded(old[name], branch, repo)
            assert (wt / "a.txt").read_text() == "a\n"

    # Resolving and re-running finishes everything, as before.
    (bad / "shared.txt").write_text("resolved\n")
    git("add", "shared.txt", cwd=bad)
    sync_stack(root=bad, forge=forge)
    for name in ("b1", "b2", "b3"):
        assert lineage.get_parent(f"devon/{name}", root=repo) == "main"
        assert git("diff", "--name-only", "main", f"devon/{name}", cwd=repo)


def test_sibling_children_of_the_landed_parent_are_each_merged(
    repo: Path, tmp_path: Path
):
    a = _stacked(
        "a", parent_wt=repo, tmp_path=tmp_path, repo=repo, files={"a.txt": "a\n"}
    )
    b = _stacked("b", parent_wt=a, tmp_path=tmp_path, repo=repo, files={"b.txt": "b\n"})
    d = _stacked("d", parent_wt=a, tmp_path=tmp_path, repo=repo, files={"d.txt": "d\n"})
    old = {"b": _tip("devon/b", repo), "d": _tip("devon/d", repo)}
    old_trees = {n: _tree(f"devon/{n}", repo) for n in "bd"}
    _land_on_main(repo, {"a.txt": "a\n"}, "squash a")

    sync_stack(root=b, forge=FakeForge(merged={"devon/a"}))

    for name, wt in (("b", b), ("d", d)):
        branch = f"devon/{name}"
        assert lineage.get_parent(branch, root=repo) == "main"
        assert _merge_count(old[name], branch, repo) == 1
        assert _tree(branch, repo) == old_trees[name]
        assert _only_fast_forwarded(old[name], branch, repo)
        assert git("diff", "--name-only", "main", branch, cwd=repo) == f"{name}.txt"
        assert (wt / "a.txt").exists()


def test_a_conflict_resolution_that_lives_in_a_merge_commit_survives(
    repo: Path, tmp_path: Path
):
    """B edits shared.txt, main changes it, so merging main into B conflicts and
    the resolution lives only in B's merge commit. Replaying B's non-merge
    commits onto the landed main would conflict again; merging does not."""
    _commit(repo, "shared.txt", "init\n", "shared")
    git("push", "origin", "main", cwd=repo)
    a = _stacked(
        "a", parent_wt=repo, tmp_path=tmp_path, repo=repo, files={"a.txt": "a\n"}
    )
    b = _stacked(
        "b", parent_wt=a, tmp_path=tmp_path, repo=repo, files={"shared.txt": "b\n"}
    )
    _land_on_main(repo, {"shared.txt": "main\n"}, "main edits shared")
    with pytest.raises(typer.Exit):
        sync_stack(root=b, forge=FakeForge())
    (b / "shared.txt").write_text("resolved\n")
    git("add", "shared.txt", cwd=b)
    sync_stack(root=b, forge=FakeForge())
    assert (b / "shared.txt").read_text() == "resolved\n"
    old_b = _tip("devon/b", repo)
    _land_on_main(repo, {"a.txt": "a\n"}, "squash a")

    sync_stack(root=b, forge=FakeForge(merged={"devon/a"}))

    assert lineage.get_parent("devon/b", root=repo) == "main"
    assert (b / "shared.txt").read_text() == "resolved\n"
    assert _only_fast_forwarded(old_b, "devon/b", repo)
    assert _tip("devon/b", repo) == _tip("origin/devon/b", repo)
    assert git("merge-base", "devon/b", "main", cwd=repo) == _tip("main", repo)


def test_restacking_twice_changes_nothing(repo: Path, tmp_path: Path):
    wts = _chain(repo, tmp_path)
    _land_on_main(repo, {"a.txt": "a\n"}, "squash a")
    forge = FakeForge(merged={"devon/a"})
    sync_stack(root=wts["c"], forge=forge)
    tips = {n: _tip(f"devon/{n}", repo) for n in "bc"}

    # The second run only publishes c's local merge (an existing fast-forward
    # rule for a branch ahead of origin); no commit is made or rewritten.
    sync_stack(root=wts["c"], forge=forge)
    assert {n: _tip(f"devon/{n}", repo) for n in "bc"} == tips
    remote_tips = {n: _tip(f"origin/devon/{n}", repo) for n in "bc"}
    assert remote_tips == tips

    sync_stack(root=wts["c"], forge=forge)
    assert {n: _tip(f"devon/{n}", repo) for n in "bc"} == tips
    assert {n: _tip(f"origin/devon/{n}", repo) for n in "bc"} == remote_tips
    assert git("status", "--porcelain", cwd=wts["c"]) == ""
