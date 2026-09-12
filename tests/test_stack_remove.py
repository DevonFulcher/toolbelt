"""Core-level tests for the `remove` command's lineage cleanup.

CLI-level happy path is covered in test_cli.py; these exercise
delete_branch_and_worktree + lineage.remove_parent directly, including the
bare-name (no `devon/` prefix) resolution path the CLI test doesn't hit.
"""

from pathlib import Path

from conftest import git

from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.worktrees_ops import delete_branch_and_worktree


def test_remove_clears_lineage_for_a_bare_name(repo: Path, tmp_path: Path):
    wt_path = tmp_path / "wt-feature"
    create_stacked_branch("feature", root=repo, wt_path=wt_path)
    assert lineage.get_parent("devon/feature", root=repo) == "main"

    # pass the bare name, as a user typing `git remove feature` would
    deleted = delete_branch_and_worktree("feature", repo_root=repo, force=True)
    lineage.remove_parent(deleted, root=repo)

    assert deleted == "devon/feature"
    assert lineage.get_parent("devon/feature", root=repo) is None
    assert (
        "devon/feature"
        not in git("branch", "--format=%(refname:short)", cwd=repo).splitlines()
    )


def test_remove_of_untracked_branch_does_not_error(repo: Path, tmp_path: Path):
    wt_path = tmp_path / "wt-scratch"
    git("worktree", "add", "-b", "scratch", str(wt_path), cwd=repo)

    deleted = delete_branch_and_worktree("scratch", repo_root=repo, force=True)
    # remove_parent is documented as a no-op for an untracked branch.
    lineage.remove_parent(deleted, root=repo)

    assert deleted == "scratch"
    assert lineage.get_parent("scratch", root=repo) is None
    assert (
        "scratch"
        not in git("branch", "--format=%(refname:short)", cwd=repo).splitlines()
    )
