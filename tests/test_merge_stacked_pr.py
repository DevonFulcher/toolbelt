"""`git_merge` refuses PRs in a GitHub stack and points at the unlink command.

`gh` and the stack sync are faked: the regular `gh pr merge` fails the way
GitHub rejects stacked PRs.
"""

import json
import subprocess
from pathlib import Path

import pytest
import typer

from toolbelt.git import workflow
from toolbelt.git.worktrees_ops import WorktreeInUseError

STACKED_ERROR = (
    "GraphQL: This pull request is part of a stack and must be merged using the "
    "asynchronous merge REST API. (mergePullRequest)"
)
VIEWED = {
    "headRefName": "feature",
    "number": 7,
    "id": "PR_1",
    "createdAt": "2026-10-01T00:00:00Z",
}


class Gh:
    def __init__(self, *, merge_stderr: str, api_responses: list[dict]):
        self.merge_stderr = merge_stderr
        self.api_responses = api_responses
        self.api_calls: list[list[str]] = []
        self.synced: list[tuple[Path, str]] = []

    def run(self, cmd, **kwargs):
        assert cmd[:3] == ["gh", "pr", "merge"]
        return subprocess.CompletedProcess(
            cmd, 1 if self.merge_stderr else 0, "", self.merge_stderr
        )

    def capture(self, cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            return json.dumps(VIEWED)
        self.api_calls.append(cmd)
        return json.dumps(self.api_responses.pop(0))


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    return tmp_path / "feature"


def _install(monkeypatch: pytest.MonkeyPatch, gh: Gh, worktree: Path) -> None:
    from toolbelt.git.stack import lineage
    from toolbelt.git.stack import worktree as stack_worktree

    monkeypatch.setattr(workflow.logged_process, "run", gh.run)
    monkeypatch.setattr(workflow, "capture", gh.capture)
    monkeypatch.setattr(workflow, "get_current_repo_root_path", lambda: worktree.parent)
    monkeypatch.setattr(lineage, "link_pr", lambda *a, **k: None)
    monkeypatch.setattr(
        stack_worktree, "worktree_paths", lambda root: {"feature": worktree}
    )
    monkeypatch.setattr(
        workflow, "sync_repo", lambda root, branch: gh.synced.append((root, branch))
    )
    monkeypatch.setattr(workflow, "main_worktree", lambda root: worktree.parent)
    monkeypatch.setattr(workflow, "ensure_not_inside_worktree", lambda *a, **k: None)


def test_stacked_pr_is_refused_with_the_unlink_command(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, caplog: pytest.LogCaptureFixture
):
    gh = Gh(merge_stderr=STACKED_ERROR, api_responses=[])
    _install(monkeypatch, gh, worktree)

    with pytest.raises(typer.Exit):
        workflow.git_merge("7")

    assert "toolbelt git unlink-github-stack 7" in caplog.text
    assert gh.api_calls == []
    assert gh.synced == []


def test_regular_merge_does_not_use_the_async_api(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr="", api_responses=[])
    _install(monkeypatch, gh, worktree)

    workflow.git_merge("7")

    assert gh.api_calls == []
    assert gh.synced == [(worktree.parent, "feature")]


def test_other_merge_failures_exit_without_syncing(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr="Pull request is not mergeable", api_responses=[])
    _install(monkeypatch, gh, worktree)

    with pytest.raises(typer.Exit):
        workflow.git_merge("7")

    assert gh.api_calls == []
    assert gh.synced == []


def test_running_inside_the_branchs_worktree_is_refused_before_merging(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr="", api_responses=[])
    _install(monkeypatch, gh, worktree)
    merges = []
    monkeypatch.setattr(
        workflow.logged_process, "run", lambda *a, **k: merges.append(a)
    )

    def refuse(*args, **kwargs):
        raise WorktreeInUseError("inside it")

    monkeypatch.setattr(workflow, "ensure_not_inside_worktree", refuse)

    with pytest.raises(WorktreeInUseError):
        workflow.git_merge("7")

    assert merges == [] and gh.synced == []
