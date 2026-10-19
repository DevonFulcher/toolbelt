"""When `sync` publishes a branch: new branches, and untracked branches of
personal repos. Real git against a local bare origin; the owner of the remote
is stubbed because ``git remote get-url`` expands ``url.<base>.insteadOf``, so a
local bare repo cannot also read as a GitHub URL."""

import logging
import subprocess
from pathlib import Path

import pytest
import typer
from conftest import FakeForge, git

from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.sync import sync_stack

PERSONAL_OWNER = "DevonFulcher"
WORK_OWNER = "dbt-labs"


def _github_origin(monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
    """Make ``origin`` read as owned by ``owner`` on GitHub."""
    monkeypatch.setattr(
        "toolbelt.tasks.requirement.origin_owner", lambda root: owner.lower()
    )


def _commit(path: Path, name: str) -> None:
    (path / name).write_text(f"{name}\n")
    git("add", "-A", cwd=path)
    git("commit", "-m", name, cwd=path)


def _origin_tip(git_remote: Path, branch: str) -> str:
    out = git("ls-remote", "--heads", str(git_remote), branch, cwd=git_remote)
    return out.split()[0] if out else ""


def _work_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IS_PERSONAL", raising=False)


@pytest.mark.parametrize("owner", [PERSONAL_OWNER, WORK_OWNER])
def test_sync_publishes_a_tracked_branch_origin_lacks(
    owner: str,
    repo: Path,
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    _work_repo(monkeypatch)
    _github_origin(monkeypatch, owner)
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    _commit(wt, "api.txt")
    caplog.set_level(logging.INFO)

    sync_stack(root=wt, forge=FakeForge(no_pr=["devon/api"]))

    assert _origin_tip(git_remote, "devon/api") == git("rev-parse", "HEAD", cwd=wt)
    assert git("config", "branch.devon/api.remote", cwd=wt) == "origin"
    assert "up to date locally" not in caplog.text


@pytest.mark.parametrize("owner", [PERSONAL_OWNER, WORK_OWNER])
def test_sync_publishes_an_untracked_new_branch_origin_lacks(
    owner: str,
    repo: Path,
    git_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _work_repo(monkeypatch)
    _github_origin(monkeypatch, owner)
    git("checkout", "-b", "feature", cwd=repo)
    _commit(repo, "f.txt")

    sync_stack(root=repo, forge=FakeForge())

    assert _origin_tip(git_remote, "feature") == git("rev-parse", "HEAD", cwd=repo)
    assert git("config", "branch.feature.remote", cwd=repo) == "origin"


def test_sync_fast_forwards_main_of_a_personal_repo(
    repo: Path, git_remote: Path, monkeypatch: pytest.MonkeyPatch
):
    _work_repo(monkeypatch)
    _github_origin(monkeypatch, PERSONAL_OWNER)
    _commit(repo, "a.txt")

    sync_stack(root=repo, forge=FakeForge())

    assert _origin_tip(git_remote, "main") == git("rev-parse", "HEAD", cwd=repo)


def test_sync_treats_is_personal_as_a_personal_repo(
    repo: Path, git_remote: Path, monkeypatch: pytest.MonkeyPatch
):
    _github_origin(monkeypatch, WORK_OWNER)  # IS_PERSONAL=true from conftest
    _commit(repo, "a.txt")

    sync_stack(root=repo, forge=FakeForge())

    assert _origin_tip(git_remote, "main") == git("rev-parse", "HEAD", cwd=repo)


def test_sync_with_main_equal_to_origin_in_a_personal_repo_succeeds(
    repo: Path, git_remote: Path, monkeypatch: pytest.MonkeyPatch
):
    _work_repo(monkeypatch)
    _github_origin(monkeypatch, PERSONAL_OWNER)
    before = _origin_tip(git_remote, "main")

    sync_stack(root=repo, forge=FakeForge())

    assert _origin_tip(git_remote, "main") == before


def test_sync_never_pushes_main_of_a_work_repo(
    repo: Path, git_remote: Path, monkeypatch: pytest.MonkeyPatch
):
    _work_repo(monkeypatch)
    _github_origin(monkeypatch, WORK_OWNER)
    before = _origin_tip(git_remote, "main")
    _commit(repo, "a.txt")

    with pytest.raises(typer.Exit):
        sync_stack(root=repo, forge=FakeForge())

    assert _origin_tip(git_remote, "main") == before


def test_sync_does_not_push_a_published_untracked_branch_of_a_work_repo(
    repo: Path, git_remote: Path, monkeypatch: pytest.MonkeyPatch
):
    _work_repo(monkeypatch)
    _github_origin(monkeypatch, WORK_OWNER)
    git("checkout", "-b", "feature", cwd=repo)
    git("push", "-u", "origin", "feature", cwd=repo)
    before = _origin_tip(git_remote, "feature")
    _commit(repo, "f.txt")

    with pytest.raises(typer.Exit):
        sync_stack(root=repo, forge=FakeForge())

    assert _origin_tip(git_remote, "feature") == before


def test_sync_refuses_to_force_main_of_a_personal_repo_when_origin_moved(
    repo: Path,
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    _work_repo(monkeypatch)
    _github_origin(monkeypatch, PERSONAL_OWNER)
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(git_remote), str(other)],
        check=True,
        capture_output=True,
    )
    git("config", "user.email", "o@example.com", cwd=other)
    git("config", "user.name", "Other", cwd=other)
    _commit(other, "theirs.txt")
    git("push", "origin", "main", cwd=other)
    theirs = _origin_tip(git_remote, "main")
    _commit(repo, "mine.txt")

    with pytest.raises(typer.Exit):
        sync_stack(root=repo, forge=FakeForge())

    assert _origin_tip(git_remote, "main") == theirs
    assert "diverged from origin/main" in caplog.text


def test_sync_still_skips_a_published_up_to_date_leaf_without_a_pr(
    repo: Path,
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    _work_repo(monkeypatch)
    _github_origin(monkeypatch, WORK_OWNER)
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    git("push", "-u", "origin", "devon/api", cwd=wt)
    caplog.set_level(logging.INFO)

    sync_stack(root=wt, forge=FakeForge(no_pr=["devon/api"]))

    assert "up to date locally; not pushing" in caplog.text
