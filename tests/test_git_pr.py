"""`git_pr`: the browser flow stays as is; --draft/--title/--body-file create
the PR non-interactively. A fake process runner stands in for `gh`."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer
from conftest import FakeForge, git

from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.workflow import git_pr


class FakeGh:
    """Records every command; `gh pr view` reports no PR, `gh pr create`
    prints a PR URL."""

    def __init__(self, existing_pr_url: str | None = None, create_code: int = 0):
        self.existing_pr_url = existing_pr_url
        self.create_code = create_code
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        self.commands.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            code = 0 if self.existing_pr_url else 1
            return subprocess.CompletedProcess(cmd, code, self.existing_pr_url, "")
        assert cmd[:3] == ["gh", "pr", "create"]
        return subprocess.CompletedProcess(
            cmd, self.create_code, "https://github.com/o/r/pull/7\n", ""
        )

    def creates(self) -> list[list[str]]:
        return [c for c in self.commands if c[:3] == ["gh", "pr", "create"]]


@pytest.fixture(autouse=True)
def _git_projects_workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_PROJECTS_WORKDIR", str(tmp_path / "workdir"))


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    (wt / "api.txt").write_text("x\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "api", cwd=wt)
    return wt


def test_without_flags_opens_the_browser_flow(worktree: Path):
    gh = FakeGh()

    git_pr(True, cwd=worktree, forge=FakeForge(), run_process=gh)

    assert gh.commands[0] == ["gh", "pr", "view", "--web"]
    assert gh.creates() == [["gh", "pr", "create", "--web", "--base", "main"]]


def test_flags_create_the_pr_non_interactively(
    worktree: Path, tmp_path: Path, capsys: pytest.CaptureFixture
):
    body = tmp_path / "body.md"
    body.write_text("the body\n")
    gh = FakeGh()

    git_pr(
        True,
        cwd=worktree,
        forge=FakeForge(),
        draft=True,
        title="My PR",
        body_file=body,
        run_process=gh,
    )

    assert "--web" not in [arg for cmd in gh.commands for arg in cmd]
    assert gh.creates() == [
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--title",
            "My PR",
            "--body-file",
            str(body),
            "--base",
            "main",
        ]
    ]
    assert capsys.readouterr().out.strip() == "https://github.com/o/r/pull/7"
    # The branch was not on origin yet, so it was pushed first.
    assert git("rev-parse", "devon/api", cwd=worktree) == git(
        "rev-parse", "origin/devon/api", cwd=worktree
    )


def test_title_and_body_file_without_draft_creates_a_ready_pr(
    worktree: Path, tmp_path: Path
):
    body = tmp_path / "body.md"
    body.write_text("b\n")
    gh = FakeGh()

    git_pr(
        True,
        cwd=worktree,
        forge=FakeForge(),
        title="T",
        body_file=body,
        run_process=gh,
    )

    assert "--draft" not in gh.creates()[0]


def test_existing_pr_url_is_printed_not_opened(
    worktree: Path, tmp_path: Path, capsys: pytest.CaptureFixture
):
    body = tmp_path / "body.md"
    body.write_text("b\n")
    gh = FakeGh(existing_pr_url="https://github.com/o/r/pull/3")

    git_pr(
        True,
        cwd=worktree,
        forge=FakeForge(),
        draft=True,
        title="T",
        body_file=body,
        run_process=gh,
    )

    assert gh.creates() == []
    assert "--web" not in gh.commands[0]
    assert capsys.readouterr().out.strip() == "https://github.com/o/r/pull/3"


def test_missing_body_file_fails_before_anything_runs(worktree: Path, tmp_path: Path):
    gh = FakeGh()

    with pytest.raises(typer.BadParameter, match="body.md does not exist"):
        git_pr(
            True,
            cwd=worktree,
            forge=FakeForge(),
            draft=True,
            title="T",
            body_file=tmp_path / "body.md",
            run_process=gh,
        )

    assert gh.commands == []


def test_draft_without_title_and_body_file_fails(worktree: Path):
    gh = FakeGh()

    with pytest.raises(typer.BadParameter, match="--title and --body-file"):
        git_pr(True, cwd=worktree, forge=FakeForge(), draft=True, run_process=gh)

    assert gh.commands == []


def test_failed_create_exits_with_its_code(worktree: Path, tmp_path: Path):
    body = tmp_path / "body.md"
    body.write_text("b\n")

    with pytest.raises(typer.Exit) as exit_info:
        git_pr(
            True,
            cwd=worktree,
            forge=FakeForge(),
            title="T",
            body_file=body,
            run_process=FakeGh(create_code=1),
        )

    assert exit_info.value.exit_code == 1
