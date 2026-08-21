"""Step definitions for the Gherkin stack scenarios (tests/features/stack.feature).

pytest-bdd step functions are plain pytest functions under the hood, so they
reuse the same real-git, throwaway-repo fixtures as the rest of the suite
(`repo`, the autouse `_isolate_git_env`) with no separate setup. `FakeForge`
is the same test double used in test_sync.py/test_restack.py — the forge is
the one seam the whole suite stubs; everything else is real git.
"""

from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from conftest import FakeForge, git

from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.lineage import get_parent
from toolbelt.git.stack.sync import sync_stack

scenarios("features/stack.feature")


@pytest.fixture
def worktrees(repo: Path) -> dict[str, Path]:
    """branch name -> its worktree path. "main" has no worktree of its own —
    it's the shared main worktree (`repo`)."""
    return {"main": repo}


@pytest.fixture
def landed() -> set[str]:
    """Branches a squash-merge step has landed, fed to FakeForge at sync time."""
    return set()


@given(parsers.parse('branch "{branch}" stacked on "{parent}"'))
def stack_branch(
    branch: str, parent: str, tmp_path: Path, worktrees: dict[str, Path]
) -> None:
    name = branch.split("/", 1)[
        1
    ]  # strip the "devon/" prefix create_stacked_branch adds back
    wt = tmp_path / name.replace("/", "_")
    create_stacked_branch(name, root=worktrees[parent], wt_path=wt)
    worktrees[branch] = wt


@given(parsers.parse('"{filename}" is committed on "{branch}"'))
@when(parsers.parse('"{filename}" is committed on "{branch}"'))
def commit_file(filename: str, branch: str, worktrees: dict[str, Path]) -> None:
    wt = worktrees[branch]
    (wt / filename).write_text(f"{filename} content\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", f"add {filename}", cwd=wt)


@when(parsers.parse('"{branch}" is squash-merged into "{base}"'))
def squash_merge(
    branch: str, base: str, worktrees: dict[str, Path], landed: set[str]
) -> None:
    """Simulate a squash-merge: replay the branch's changed files as one fresh
    commit on `base` (a new SHA, no shared ancestry) — the same shape a real
    GitHub squash-merge produces, and what `sync`'s restack path must detect
    via the forge rather than local ancestry."""
    branch_wt = worktrees[branch]
    base_wt = worktrees[base]
    parent = get_parent(branch, root=branch_wt)
    changed = git(
        "diff", "--name-only", f"{parent}...{branch}", cwd=branch_wt
    ).splitlines()
    for name in changed:
        (base_wt / name).write_text((branch_wt / name).read_text())
    git("add", "-A", cwd=base_wt)
    git("commit", "-m", f"squash-merge {branch}", cwd=base_wt)
    git("push", "origin", base, cwd=base_wt)
    landed.add(branch)


@when(parsers.parse('the stack is synced from "{branch}"'))
def sync_from(branch: str, worktrees: dict[str, Path], landed: set[str]) -> None:
    sync_stack(root=worktrees[branch], forge=FakeForge(merged=landed))


@then(parsers.parse('"{branch}" contains "{filename}"'))
def branch_contains(branch: str, filename: str, worktrees: dict[str, Path]) -> None:
    assert (worktrees[branch] / filename).exists()


@then(parsers.parse('"{branch}" is stacked on "{parent}"'))
def branch_stacked_on(branch: str, parent: str, worktrees: dict[str, Path]) -> None:
    assert get_parent(branch, root=worktrees[branch]) == parent


@then(parsers.parse('branch "{branch}" no longer exists'))
def branch_gone(branch: str, repo: Path) -> None:
    branches = git("branch", "--format=%(refname:short)", cwd=repo).splitlines()
    assert branch not in branches
