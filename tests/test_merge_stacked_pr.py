"""`git_merge` refuses PRs in a GitHub stack and tells the user which stack to unstack in GitHub.

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
        self.merge_attempts = []
        self.merges = 0
        self.api_calls: list[list[str]] = []
        self.synced: list[tuple[Path, str]] = []

    #: Answers of the next merges, in order, before ``merge_stderr``.
    merge_attempts: list[str]
    #: What ``gh pr view --json state`` says.
    state = "OPEN"

    def run(self, cmd, **kwargs):
        assert cmd[:3] == ["gh", "pr", "merge"]
        self.merges += 1
        stderr = (
            self.merge_attempts.pop(0) if self.merge_attempts else self.merge_stderr
        )
        return subprocess.CompletedProcess(cmd, 1 if stderr else 0, "", stderr)

    def capture(self, cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"] and "--jq" in cmd:
            return self.state
        if cmd[:3] == ["gh", "pr", "view"]:
            return json.dumps(VIEWED)
        self.api_calls.append(cmd)
        response = self.api_responses.pop(0)
        return (
            json.dumps(response) if response is not None else ""
        )  # gh prints nothing for null


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
    monkeypatch.setattr(lineage, "get_links", lambda *a, **k: None)
    monkeypatch.setattr(
        stack_worktree, "worktree_paths", lambda root: {"feature": worktree}
    )
    monkeypatch.setattr(
        workflow, "sync_repo", lambda root, branch: gh.synced.append((root, branch))
    )
    monkeypatch.setattr(workflow, "main_worktree", lambda root: worktree.parent)
    monkeypatch.setattr(workflow, "ensure_not_inside_worktree", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "_MERGE_RETRY_SECONDS", 0.0)


def test_stacked_pr_is_refused_with_the_unstack_command_for_its_stack(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, caplog: pytest.LogCaptureFixture
):
    gh = Gh(
        merge_stderr=STACKED_ERROR,
        api_responses=[{"id": 99, "number": 1812, "position": 1, "size": 2}],
    )
    _install(monkeypatch, gh, worktree)

    with pytest.raises(typer.Exit):
        workflow.git_merge("7")

    assert "PR 7 is part of GitHub stack 1812 (2 PRs)" in caplog.text
    assert "gh stack unstack 1812" in caplog.text
    assert gh.api_calls == [
        ["gh", "api", "repos/{owner}/{repo}/pulls/7", "--jq", ".stack"]
    ]
    assert gh.synced == []


def test_the_refusal_still_works_when_github_does_not_say_which_stack(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, caplog: pytest.LogCaptureFixture
):
    gh = Gh(merge_stderr=STACKED_ERROR, api_responses=[None])  # type: ignore[list-item]
    _install(monkeypatch, gh, worktree)

    with pytest.raises(typer.Exit):
        workflow.git_merge("7")

    assert "gh stack unstack <stack-number>" in caplog.text
    assert gh.synced == []


def test_regular_merge_does_not_use_the_async_api(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr="", api_responses=[])
    _install(monkeypatch, gh, worktree)

    workflow.git_merge("7")

    assert gh.api_calls == []
    assert gh.synced == [(worktree.parent, "feature")]


def test_a_merge_moves_the_branchs_task_to_done_once_github_has_merged_it(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    from toolbelt.git.stack import lineage
    from toolbelt.git.stack.store import BranchLinks

    gh = Gh(merge_stderr="", api_responses=[])
    _install(monkeypatch, gh, worktree)
    links = BranchLinks(task="RD-1", task_tracker="jira", agent_link=None)
    monkeypatch.setattr(lineage, "get_links", lambda *a, **k: links)
    finished = []
    monkeypatch.setattr(workflow, "_finish_task", finished.append)

    workflow.git_merge("7")

    assert finished == [links]
    assert gh.synced == [(worktree.parent, "feature")]


def test_a_merge_github_refused_leaves_the_task_alone(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr="Pull request is not mergeable", api_responses=[])
    _install(monkeypatch, gh, worktree)
    finished = []
    monkeypatch.setattr(workflow, "_finish_task", finished.append)

    with pytest.raises(typer.Exit):
        workflow.git_merge("7")

    assert finished == []


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


TRANSIENT_ERROR = (
    "GraphQL: Something went wrong while executing your query on "
    "2026-10-23T16:51:06Z. Please include `DCE2:2E969C` when reporting this issue."
)


def test_a_merge_github_failed_on_its_own_is_tried_again(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr="", api_responses=[])
    gh.merge_attempts = [TRANSIENT_ERROR, TRANSIENT_ERROR]
    _install(monkeypatch, gh, worktree)

    workflow.git_merge("7")

    assert gh.merges == 3
    assert gh.synced == [(worktree.parent, "feature")]


def test_it_gives_up_when_github_keeps_failing(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr=TRANSIENT_ERROR, api_responses=[])
    _install(monkeypatch, gh, worktree)

    with pytest.raises(typer.Exit):
        workflow.git_merge("7")

    assert gh.merges == workflow._MERGE_ATTEMPTS
    assert gh.synced == []


def test_a_pr_github_merged_before_it_failed_is_not_merged_twice(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
):
    gh = Gh(merge_stderr="", api_responses=[])
    gh.merge_attempts = [TRANSIENT_ERROR]
    gh.state = "MERGED"
    _install(monkeypatch, gh, worktree)

    workflow.git_merge("7")

    assert gh.merges == 1
    assert gh.synced == [(worktree.parent, "feature")]


def test_a_refusal_is_not_tried_again(monkeypatch: pytest.MonkeyPatch, worktree: Path):
    gh = Gh(merge_stderr="Pull request is not mergeable", api_responses=[])
    _install(monkeypatch, gh, worktree)

    with pytest.raises(typer.Exit):
        workflow.git_merge("7")

    assert gh.merges == 1
