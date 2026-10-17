"""Where a branch's conflict comes from, found with real git in tmp."""

import subprocess
from pathlib import Path

from conftest import git

from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.ui.conflict_commit import conflict_for, find_pr_conflict


def _commit(worktree: Path, filename: str, content: str, message: str) -> str:
    (worktree / filename).write_text(content)
    git("add", "-A", cwd=worktree)
    git("commit", "-m", message, cwd=worktree)
    return git("rev-parse", "HEAD", cwd=worktree)


def _feature_conflicting_on_its_second_commit(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-feat"
    create_stacked_branch("feat", root=repo, wt_path=wt)
    _commit(wt, "g.txt", "g\n", "own one")
    conflicting = _commit(wt, "README.md", "feature\n", "own two edits readme")
    _commit(wt, "h.txt", "h\n", "own three")
    (repo / "README.md").write_text("main\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "main edits readme", cwd=repo)
    return wt, conflicting


def test_the_first_commit_whose_changes_conflict_with_the_base_is_found(
    repo: Path, tmp_path: Path
):
    _wt, conflicting = _feature_conflicting_on_its_second_commit(repo, tmp_path)

    found = find_pr_conflict(repo, "main", "devon/feat")

    assert found is not None
    assert (found.sha, found.subject, found.source) == (
        conflicting,
        "own two edits readme",
        "pr",
    )
    assert found.files == ("README.md",)


def test_a_branch_that_merges_cleanly_has_no_conflict_commit(
    repo: Path, tmp_path: Path
):
    wt = tmp_path / "wt-clean"
    create_stacked_branch("clean", root=repo, wt_path=wt)
    _commit(wt, "only.txt", "x\n", "own")

    assert find_pr_conflict(repo, "main", "devon/clean") is None


def test_a_conflicting_pr_is_resolved_against_the_remote_base_and_cached(
    repo: Path, tmp_path: Path
):
    wt, conflicting = _feature_conflicting_on_its_second_commit(repo, tmp_path)
    git("push", "origin", "main", cwd=repo)
    head = git("rev-parse", "devon/feat", cwd=wt)

    first = conflict_for(
        repo, "devon/feat", base_ref="main", head_oid=head, pr_conflicts=True
    )
    second = conflict_for(
        repo, "devon/feat", base_ref="main", head_oid=head, pr_conflicts=True
    )

    assert first is not None and first.sha == conflicting
    assert second is first


def test_a_pr_that_does_not_conflict_shows_nothing(repo: Path, tmp_path: Path):
    wt, _ = _feature_conflicting_on_its_second_commit(repo, tmp_path)
    head = git("rev-parse", "devon/feat", cwd=wt)

    assert (
        conflict_for(
            repo, "devon/feat", base_ref="main", head_oid=head, pr_conflicts=False
        )
        is None
    )


def test_a_worktree_stopped_on_a_rebase_conflict_names_the_commit_being_applied(
    repo: Path, tmp_path: Path
):
    wt, conflicting = _feature_conflicting_on_its_second_commit(repo, tmp_path)
    git("push", "origin", "main", cwd=repo)
    subprocess.run(["git", "rebase", "main"], cwd=wt, capture_output=True, check=False)

    found = conflict_for(
        repo, "devon/feat", base_ref=None, head_oid=None, pr_conflicts=False
    )

    assert found is not None
    assert (found.sha, found.source) == (conflicting, "rebase")
    assert (found.step, found.steps) == (2, 3)
    assert found.files == ("README.md",)
