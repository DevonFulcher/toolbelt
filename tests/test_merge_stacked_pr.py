"""`git_merge` falls back to GitHub's async merge API for PRs in a GitHub stack.

`gh` and the stack sync are faked: the regular `gh pr merge` fails the way
GitHub rejects stacked PRs, and the `gh api` calls return scripted responses.
"""

import json
import subprocess
from pathlib import Path

import pytest
import typer

from toolbelt.git import workflow

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
        self.synced: list[Path] = []

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
    monkeypatch.setattr(workflow, "sync_repo", gh.synced.append)
    monkeypatch.setattr(workflow.time, "sleep", lambda seconds: None)


def test_stacked_pr_is_merged_through_the_async_api_then_synced(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(
        merge_stderr=STACKED_ERROR,
        api_responses=[
            {"status": "pending", "details": {"uuid": "abc"}},
            {"status": "pending", "details": {"uuid": "abc"}},
            {"status": "merged", "details": {"sha": "deadbeef"}},
        ],
    )
    _install(monkeypatch, gh, worktree)

    workflow.git_merge("7")

    assert gh.api_calls[0] == [
        "gh",
        "api",
        "-X",
        "PUT",
        "repos/{owner}/{repo}/pulls/7/merge-async",
        "-f",
        "merge_method=squash",
    ]
    assert (
        gh.api_calls[1:]
        == [["gh", "api", "repos/{owner}/{repo}/pulls/7/merge-async/abc"]] * 2
    )
    assert gh.synced == [worktree]


def test_failed_async_merge_exits_without_syncing(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(
        merge_stderr=STACKED_ERROR,
        api_responses=[{"status": "failed", "details": {"message": "checks"}}],
    )
    _install(monkeypatch, gh, worktree)

    with pytest.raises(typer.Exit):
        workflow.git_merge("7")

    assert gh.synced == []


def test_queued_async_merge_does_not_sync(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(
        merge_stderr=STACKED_ERROR,
        api_responses=[{"status": "enqueued", "details": {"message": "queued"}}],
    )
    _install(monkeypatch, gh, worktree)

    workflow.git_merge("7")

    assert gh.synced == []


def test_regular_merge_does_not_use_the_async_api(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr="", api_responses=[])
    _install(monkeypatch, gh, worktree)

    workflow.git_merge("7")

    assert gh.api_calls == []
    assert gh.synced == [worktree]


def test_other_merge_failures_exit_without_syncing(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr="Pull request is not mergeable", api_responses=[])
    _install(monkeypatch, gh, worktree)

    with pytest.raises(typer.Exit):
        workflow.git_merge("7")

    assert gh.api_calls == []
    assert gh.synced == []
