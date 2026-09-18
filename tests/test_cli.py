"""CLI-level smoke tests: invoke the actual typer commands, not just their cores.

These close a real gap the core-level tests (test_sync.py, test_ops.py, ...)
don't cover: argument parsing, flag handling, and orchestration inside the
`@git_typer.command` functions and `git_save`/`sync_repo` in workflow.py. That
gap is what let a real bug through once already (the parent-conflict check
read the wrong parent source) even though every core function was tested.

Commands that reach `GhForge` (a live `gh` call) — `sync`, `set-parent` — are
deliberately NOT invoked here; that path is already covered at the core level
via `sync_stack` + `FakeForge` in test_sync.py/test_restack.py. Exercising it
through the CLI would make these tests depend on `gh` auth and network state.
`switch` opens an editor; `open_in_editor` is monkeypatched to a no-op
so it can run headless.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import git

from toolbelt.git.cli import git_typer
from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_real_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    """`switch` calls `open_in_editor`; stub it out for headless CI."""
    monkeypatch.setattr("toolbelt.git.stack.cli.open_in_editor", lambda _path: None)


@pytest.fixture(autouse=True)
def _git_projects_workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`save` (via store_commit) and `append` (via worktree paths) both need
    GIT_PROJECTS_WORKDIR set; point it at a throwaway dir for every test."""
    monkeypatch.setenv("GIT_PROJECTS_WORKDIR", str(tmp_path / "git-projects-workdir"))


@dataclass
class Invocation:
    exit_code: int
    output: str  # combined stdout+stderr captured at the fd level


def _invoke(args: list[str], *, cwd: Path, capfd: pytest.CaptureFixture) -> Invocation:
    """Invoke a `git_typer` command and capture its real console output.

    `toolbelt`'s logger binds a `StreamHandler` to whatever `sys.stdout` object
    is current at *module-import* time (see logger.py) — i.e. at pytest
    collection, before any per-test capture fixture exists. Click's CliRunner
    also only swaps `sys.stdout` at the Python-object level (its own
    `_NamedTextIOWrapper`), so `result.output` never sees our logger either.
    Re-running `setup_logging()` right before invoking rebinds the handler to
    *this test's* current `sys.stdout`, which is what `capfd` is tracking —
    then `capfd.readouterr()` reliably picks it up.
    """
    from toolbelt.logger import setup_logging

    capfd.readouterr()  # drain anything buffered from setup
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(cwd)
        setup_logging()
        result = runner.invoke(git_typer, args)
    captured = capfd.readouterr()
    return Invocation(exit_code=result.exit_code, output=captured.out + captured.err)


# --- save --------------------------------------------------------------


def test_save_commits_on_untracked_branch(repo: Path, capfd: pytest.CaptureFixture):
    git("checkout", "-q", "-b", "loose", cwd=repo)
    (repo / "f.txt").write_text("x\n")

    result = _invoke(["save", "-m", "add f", "--no-sync"], cwd=repo, capfd=capfd)

    assert result.exit_code == 0, result.output
    assert git("log", "-1", "--format=%s", cwd=repo) == "add f"
    assert git("status", "--porcelain", cwd=repo) == ""


def test_save_warns_on_real_parent_conflict(
    repo: Path, tmp_path: Path, capfd: pytest.CaptureFixture
):
    """Regression test: `git save` must warn using the *lineage* parent, not
    the branch's own upstream (the bug fixed in 673c900). The check runs
    against HEAD, so the conflicting change must already be committed on the
    branch *before* this save call — a new, unrelated change then triggers the
    check, which should flag the pre-existing conflict with main."""
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)

    (wt / "c.txt").write_text("API\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "api touches c.txt", cwd=wt)

    (repo / "c.txt").write_text("MAIN\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "main touches c.txt", cwd=repo)

    (wt / "extra.txt").write_text("unrelated\n")

    result = _invoke(
        ["save", "-m", "add extra", "--no-sync", "--yes"], cwd=wt, capfd=capfd
    )

    assert result.exit_code == 0, result.output
    assert "may create merge conflicts" in result.output
    assert git("log", "-1", "--format=%s", cwd=wt) == "add extra"


def test_save_requires_message_or_amend(repo: Path, capfd: pytest.CaptureFixture):
    result = _invoke(["save"], cwd=repo, capfd=capfd)
    assert result.exit_code != 0


# --- diff-parent ---------------------------------------------------------


def test_diff_parent_shows_only_branch_changes(
    repo: Path, tmp_path: Path, capfd: pytest.CaptureFixture
):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    (wt / "new.txt").write_text("hello\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "add new.txt", cwd=wt)

    result = _invoke(["diff-parent"], cwd=wt, capfd=capfd)

    assert result.exit_code == 0, result.output
    # Assert on the added content, not just the filename: if difftastic were
    # missing/broken, git's own error ("external diff died, stopping at
    # new.txt") would still contain "new.txt" and false-positive this test.
    assert "hello" in result.output


def test_diff_parent_errors_on_untracked_branch(
    repo: Path, capfd: pytest.CaptureFixture
):
    git("checkout", "-q", "-b", "loose", cwd=repo)
    result = _invoke(["diff-parent"], cwd=repo, capfd=capfd)
    assert result.exit_code != 0


# --- compress --------------------------------------------------------------


def test_compress_squashes_via_cli(
    repo: Path, tmp_path: Path, capfd: pytest.CaptureFixture
):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    for msg in ("one", "two"):
        (wt / f"{msg}.txt").write_text("x\n")
        git("add", "-A", cwd=wt)
        git("commit", "-m", msg, cwd=wt)

    result = _invoke(["compress", "-m", "squashed"], cwd=wt, capfd=capfd)

    assert result.exit_code == 0, result.output
    assert int(git("rev-list", "--count", "main..HEAD", cwd=wt)) == 1
    assert git("log", "-1", "--format=%s", cwd=wt) == "squashed"


# --- compare ---------------------------------------------------------------


def test_compare_defaults_to_difftastic(repo: Path, capfd: pytest.CaptureFixture):
    """`compare` is view-only, so it defaults to difftastic's AST-aware diff
    (installed alongside toolbelt by the dotfiles bootstrap)."""
    (repo / "README.md").write_text("init\nchanged\n")

    result = _invoke(["compare"], cwd=repo, capfd=capfd)

    assert result.exit_code == 0, result.output
    assert "README.md" in result.output


def test_compare_line_falls_back_to_plain_diff(
    repo: Path, capfd: pytest.CaptureFixture
):
    """`--line` forces a plain unified diff instead of difftastic."""
    (repo / "README.md").write_text("init\nchanged\n")

    result = _invoke(["compare", "--line"], cwd=repo, capfd=capfd)

    assert result.exit_code == 0, result.output
    # The `diff --git` header only appears in git's own unified diff, never in
    # difftastic's structural rendering.
    assert "diff --git" in result.output


def test_compare_accepts_a_path_argument(repo: Path, capfd: pytest.CaptureFixture):
    """A file path passed to `compare` is treated as a pathspec, not a revision
    (previously `compare <path>` failed with `bad revision`)."""
    (repo / "other.txt").write_text("orig\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add other.txt", cwd=repo)
    # Modify two tracked files; the path arg should scope the diff to one.
    (repo / "README.md").write_text("init\nchanged\n")
    (repo / "other.txt").write_text("changed\n")

    result = _invoke(["compare", "--line", "README.md"], cwd=repo, capfd=capfd)

    assert result.exit_code == 0, result.output
    assert "README.md" in result.output
    assert "other.txt" not in result.output


# --- combine / change -------------------------------------------------------


def test_combine_merges_a_branch(repo: Path, capfd: pytest.CaptureFixture):
    git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "feature.txt").write_text("f\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add feature.txt", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)

    result = _invoke(["combine", "feature"], cwd=repo, capfd=capfd)

    assert result.exit_code == 0, result.output
    assert (repo / "feature.txt").exists()


def test_change_creates_new_branch(repo: Path, capfd: pytest.CaptureFixture):
    result = _invoke(["change", "-b", "spike"], cwd=repo, capfd=capfd)

    assert result.exit_code == 0, result.output
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "spike"


# --- append / switch / tree (switch's editor stubbed) -----------------------


def test_append_creates_worktree_via_cli(repo: Path, capfd: pytest.CaptureFixture):
    result = _invoke(["append", "feature"], cwd=repo, capfd=capfd)

    assert result.exit_code == 0, result.output
    assert lineage.get_parent("devon/feature", root=repo) == "main"


def test_remove_deletes_branch_worktree_and_lineage_via_cli(
    repo: Path, capfd: pytest.CaptureFixture
):
    _invoke(["append", "feature"], cwd=repo, capfd=capfd)
    assert lineage.get_parent("devon/feature", root=repo) == "main"

    result = _invoke(["remove", "devon/feature", "--force"], cwd=repo, capfd=capfd)

    assert result.exit_code == 0, result.output
    assert lineage.get_parent("devon/feature", root=repo) is None
    assert (
        "devon/feature"
        not in git("branch", "--format=%(refname:short)", cwd=repo).splitlines()
    )


def test_tree_reports_no_stacks_when_untracked(
    repo: Path, capfd: pytest.CaptureFixture
):
    result = _invoke(["tree"], cwd=repo, capfd=capfd)
    assert result.exit_code == 0
    assert "No tracked stacks" in result.output


def test_tree_renders_a_stack(repo: Path, tmp_path: Path, capfd: pytest.CaptureFixture):
    create_stacked_branch("api", root=repo, wt_path=tmp_path / "wt-api")

    result = _invoke(["tree"], cwd=repo, capfd=capfd)

    assert result.exit_code == 0, result.output
    assert "devon/api" in result.output
    assert "main" in result.output
