"""Unit tests for `git_branch_clean`'s stack lineage cleanup."""

from pathlib import Path

from conftest import git

from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.workflow import git_branch_clean


def test_deletes_branch_and_drops_its_own_lineage_entry(repo: Path, tmp_path: Path):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    git("push", "-u", "origin", "devon/api", cwd=api_wt)
    # Simulate the PR landing and GitHub auto-deleting the head branch.
    git("push", "origin", "--delete", "devon/api", cwd=api_wt)

    git_branch_clean(root=repo)

    assert lineage.get_parent("devon/api", root=repo) is None
    assert (
        "devon/api"
        not in git("branch", "--format=%(refname:short)", cwd=repo).splitlines()
    )


def test_reparents_tracked_children_onto_the_deleted_branchs_parent(
    repo: Path, tmp_path: Path
):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    create_stacked_branch("api_tests", root=api_wt, wt_path=tmp_path / "wt-api-tests")
    git("push", "-u", "origin", "devon/api", cwd=api_wt)
    git("push", "origin", "--delete", "devon/api", cwd=api_wt)

    git_branch_clean(root=repo)

    # "devon/api" is gone; its child is repointed to "devon/api"'s own
    # parent ("main") rather than left pointing at a branch that no longer
    # exists.
    assert lineage.get_parent("devon/api_tests", root=repo) == "main"


def test_works_when_root_is_the_worktree_being_deleted(repo: Path, tmp_path: Path):
    """`root` can be the very worktree whose branch just landed. Deletion must
    still run from the main worktree (see `main_worktree`) since git can't
    remove a worktree with cwd pointed at the worktree being removed."""
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    create_stacked_branch("api_tests", root=api_wt, wt_path=tmp_path / "wt-api-tests")
    git("push", "-u", "origin", "devon/api", cwd=api_wt)
    git("push", "origin", "--delete", "devon/api", cwd=api_wt)

    git_branch_clean(root=api_wt)

    assert not api_wt.exists()
    assert lineage.get_parent("devon/api", root=repo) is None
    assert lineage.get_parent("devon/api_tests", root=repo) == "main"


def test_drops_lineage_entry_for_a_branch_already_deleted_by_other_means(
    repo: Path, tmp_path: Path
):
    """A branch deleted directly (e.g. `git branch -D`), bypassing this tool
    entirely, leaves no "gone" upstream for `git branch -vv` to catch — it
    just doesn't exist anymore. Only a sweep over lineage entries themselves
    catches this; without it, `git tree` shows the branch as a ghost node
    forever."""
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    create_stacked_branch("api_tests", root=api_wt, wt_path=tmp_path / "wt-api-tests")
    git("worktree", "remove", "--force", str(api_wt), cwd=repo)
    git("branch", "-D", "devon/api", cwd=repo)

    git_branch_clean(root=repo)

    assert lineage.get_parent("devon/api", root=repo) is None
    assert lineage.get_parent("devon/api_tests", root=repo) == "main"


def test_leaves_untracked_gone_branches_alone_beyond_deleting_them(
    repo: Path, tmp_path: Path
):
    git("checkout", "-b", "scratch", cwd=repo)
    git("push", "-u", "origin", "scratch", cwd=repo)
    git("checkout", "main", cwd=repo)
    git("push", "origin", "--delete", "scratch", cwd=repo)

    git_branch_clean(root=repo)

    assert (
        "scratch"
        not in git("branch", "--format=%(refname:short)", cwd=repo).splitlines()
    )
    assert lineage.get_parent("scratch", root=repo) is None
