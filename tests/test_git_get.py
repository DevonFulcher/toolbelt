"""`git get` preserves the clone and original error when setup fails."""

import subprocess
import traceback
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolbelt.git.cli import git_typer


@pytest.mark.parametrize("already_exists", [False, True])
@pytest.mark.parametrize(
    "make_error",
    [
        lambda: subprocess.CalledProcessError(1, ["uv", "sync"]),
        lambda: FileNotFoundError("asdf not found"),
        lambda: ValueError("invalid setup configuration"),
    ],
)
def test_setup_failure_preserves_clone_and_traceback_and_can_be_retried(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_exists: bool,
    make_error: Callable[[], Exception],
) -> None:
    error = make_error()
    workdir = tmp_path / "projects"
    workdir.mkdir()
    monkeypatch.setenv("GIT_PROJECTS_WORKDIR", str(workdir))
    clone_path = workdir / "origin"
    opened: list[Path] = []
    setup_paths: list[tuple[Path, Path | None, str | None]] = []

    def setup(
        target_path: Path,
        git_projects_workdir: Path | None = None,
        service_name: str | None = None,
    ) -> None:
        setup_paths.append((target_path, git_projects_workdir, service_name))

    def fail_setup(
        target_path: Path,
        git_projects_workdir: Path | None = None,
        service_name: str | None = None,
    ) -> None:
        setup(target_path, git_projects_workdir, service_name)
        (target_path / "setup-progress").write_text("preserve me\n")
        raise error

    monkeypatch.setattr("toolbelt.git.cli.git_setup", setup)
    monkeypatch.setattr("toolbelt.git.cli.open_in_editor", opened.append)
    runner = CliRunner()
    args = ["get", str(git_remote), "--service-name", "my-service"]
    if already_exists:
        assert runner.invoke(git_typer, args).exit_code == 0
        opened.clear()
        setup_paths.clear()

    monkeypatch.setattr("toolbelt.git.cli.git_setup", fail_setup)
    result = runner.invoke(git_typer, args)

    assert result.exit_code != 0
    assert result.exception is error
    assert error.__cause__ is None
    frames = traceback.extract_tb(error.__traceback__)
    assert frames[-1].name == "fail_setup"
    assert sum(frame.name == "get" for frame in frames) == 1
    status = "Repository already exists" if already_exists else "Clone succeeded"
    assert f"{status} at {clone_path}, but setup failed." in result.stderr
    assert "The clone has been preserved." in result.stderr
    assert "Fix the setup error below" in result.stderr
    assert "rerun the same `tt git get` command" in result.stderr
    assert "without cloning again" in result.stderr
    assert opened == []
    assert (clone_path / ".git").is_dir()
    assert (clone_path / "setup-progress").read_text() == "preserve me\n"
    assert setup_paths == [(clone_path, workdir, "my-service")]

    # Retry with setup repaired; the existing clone and setup progress survive.
    monkeypatch.setattr("toolbelt.git.cli.git_setup", setup)
    retry = runner.invoke(git_typer, args)

    assert retry.exit_code == 0
    assert retry.stderr == ""
    assert opened == [clone_path]
    assert (clone_path / "setup-progress").read_text() == "preserve me\n"
    assert setup_paths == [(clone_path, workdir, "my-service")] * 2


def test_successful_clone_and_setup_opens_editor(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_PROJECTS_WORKDIR", str(tmp_path))
    clone_path = tmp_path / "origin"
    opened: list[Path] = []

    def setup(
        target_path: Path,
        git_projects_workdir: Path | None = None,
        service_name: str | None = None,
    ) -> None:
        assert git_projects_workdir == tmp_path
        assert service_name is None
        (target_path / "setup-complete").touch()

    monkeypatch.setattr("toolbelt.git.cli.git_setup", setup)
    monkeypatch.setattr("toolbelt.git.cli.open_in_editor", opened.append)

    result = CliRunner().invoke(git_typer, ["get", str(git_remote)])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert (clone_path / ".git").is_dir()
    assert (clone_path / "setup-complete").exists()
    assert opened == [clone_path]
