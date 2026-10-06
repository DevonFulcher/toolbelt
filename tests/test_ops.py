"""Integration tests for compress / diff-parent / set-parent cores."""

from pathlib import Path

import pytest
import typer

from conftest import git

from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.ops import (
    compress_branch,
    diff_parent_command,
    set_branch_parent,
)


def _commits_since(ref: str, *, cwd: Path) -> int:
    return int(git("rev-list", "--count", f"{ref}..HEAD", cwd=cwd))


# --- compress ---------------------------------------------------------------


def test_compress_squashes_branch_commits_into_one(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    for i in range(3):
        (wt / f"f{i}.txt").write_text(f"{i}\n")
        git("add", "-A", cwd=wt)
        git("commit", "-m", f"work {i}", cwd=wt)
    assert _commits_since("main", cwd=wt) == 3

    compress_branch(root=wt, message="squashed")

    assert _commits_since("main", cwd=wt) == 1
    assert git("log", "-1", "--format=%s", cwd=wt) == "squashed"
    # All the work is still present.
    for i in range(3):
        assert (wt / f"f{i}.txt").exists()


def test_compress_defaults_to_first_commit_subject(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    # Clean repo, so no "WIP" carry commit; these two are the branch's commits.
    for msg in ("first real", "second real"):
        (wt / f"{msg.replace(' ', '_')}.txt").write_text("x\n")
        git("add", "-A", cwd=wt)
        git("commit", "-m", msg, cwd=wt)

    compress_branch(root=wt, message=None)

    assert _commits_since("main", cwd=wt) == 1
    # Default message is the branch's first (oldest) commit subject.
    assert git("log", "-1", "--format=%s", cwd=wt) == "first real"


def _commit_file(wt: Path, name: str, message: str) -> None:
    (wt / name).write_text("x\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", message, cwd=wt)


def _squashed_message(wt: Path) -> str:
    return git("log", "-1", "--format=%B", cwd=wt)


def test_compress_default_message_keeps_body_and_trailer(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    first = (
        "Declare every third-party package imported directly\n\n"
        "# Why\n"
        "First paragraph.\n\n"
        "Second paragraph.\n\n"
        "Co-Authored-By: AI Assistant <noreply@ai>"
    )
    _commit_file(wt, "one.txt", first)
    _commit_file(wt, "two.txt", "second real")

    compress_branch(root=wt, message=None)

    assert _commits_since("main", cwd=wt) == 1
    assert _squashed_message(wt) == first


def test_compress_adds_trailers_from_other_commits_once(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    _commit_file(
        wt,
        "one.txt",
        "first\n\nbody\n\nSigned-off-by: Test <test@example.com>\n"
        "Co-Authored-By: Ada <ada@example.com>",
    )
    _commit_file(
        wt,
        "two.txt",
        "second\n\nsecond body that is not carried over\n\n"
        "Co-Authored-By: Ada <ada@example.com>\n"
        "Co-Authored-By: Grace <grace@example.com>",
    )
    _commit_file(wt, "three.txt", "third\n\nco-authored-by: Linus <linus@example.com>")
    _commit_file(wt, "four.txt", "fourth\n\nco-authored-by: Linus <linus@example.com>")

    compress_branch(root=wt, message=None)

    assert _commits_since("main", cwd=wt) == 1
    assert _squashed_message(wt) == (
        "first\n\nbody\n\nSigned-off-by: Test <test@example.com>\n"
        "Co-Authored-By: Ada <ada@example.com>\n"
        "Co-Authored-By: Grace <grace@example.com>\n"
        "co-authored-by: Linus <linus@example.com>"
    )


def test_compress_starts_trailer_block_when_chosen_commit_has_none(
    repo: Path, tmp_path: Path
):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    _commit_file(wt, "one.txt", "first\n\nbody")
    _commit_file(wt, "two.txt", "second\n\nCo-Authored-By: Ada <ada@example.com>")

    compress_branch(root=wt, message=None)

    assert _squashed_message(wt) == (
        "first\n\nbody\n\nCo-Authored-By: Ada <ada@example.com>"
    )


def test_compress_subject_only_commits_stay_subject_only(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    _commit_file(wt, "one.txt", "first real")
    _commit_file(wt, "two.txt", "second real")

    compress_branch(root=wt, message=None)

    assert _squashed_message(wt) == "first real"


def test_compress_explicit_multiline_message_is_used_verbatim(
    repo: Path, tmp_path: Path
):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    _commit_file(wt, "one.txt", "first\n\nCo-Authored-By: Ada <ada@example.com>")
    _commit_file(wt, "two.txt", "second")

    compress_branch(root=wt, message="explicit\n\n# not a comment")

    assert _squashed_message(wt) == "explicit\n\n# not a comment"


def test_compress_default_message_skips_merge_commits(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    (repo / "main.txt").write_text("x\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "main work", cwd=repo)
    # The merge is the branch's oldest commit past the merge base.
    git("merge", "--no-ff", "--no-edit", "main", cwd=wt)
    (wt / "own.txt").write_text("x\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "own work", cwd=wt)

    compress_branch(root=wt, message=None)

    assert _commits_since("main", cwd=wt) == 1
    assert git("log", "-1", "--format=%s", cwd=wt) == "own work"


def test_compress_default_message_without_parent_reflog(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    git("reflog", "expire", "--expire=now", "--all", cwd=repo)
    assert git("rev-list", "--walk-reflogs", "main", cwd=repo) == ""
    for msg in ("first real", "second real"):
        (wt / f"{msg.replace(' ', '_')}.txt").write_text("x\n")
        git("add", "-A", cwd=wt)
        git("commit", "-m", msg, cwd=wt)

    compress_branch(root=wt, message=None)

    assert _commits_since("main", cwd=wt) == 1
    assert git("log", "-1", "--format=%s", cwd=wt) == "first real"


def test_compress_default_message_when_only_merges_remain(
    repo: Path, tmp_path: Path, commit
):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    # Each merge also carries its own change, so the squash isn't empty.
    for name in ("one", "two"):
        commit(f"{name}.txt", "x\n", f"main {name}")
        git("merge", "--no-ff", "--no-commit", "main", cwd=wt)
        (wt / f"own_{name}.txt").write_text("x\n")
        git("add", "-A", cwd=wt)
        git("commit", "-m", f"merge {name}", cwd=wt)

    compress_branch(root=wt, message=None)

    assert _commits_since("main", cwd=wt) == 1
    assert git("log", "-1", "--format=%s", cwd=wt) == "merge one"


def _stale_local_main(repo: Path, commit) -> str:
    """Advance ``origin/main`` by one commit and leave local ``main`` behind it.

    Returns the new ``origin/main`` tip. This is the usual state of a clone whose
    stack syncs merge ``origin/main`` into branches but never update local ``main``.
    """
    old = git("rev-parse", "HEAD", cwd=repo)
    commit("newer.txt", "x\n", "newer main work")
    git("push", "origin", "main", cwd=repo)
    newer = git("rev-parse", "HEAD", cwd=repo)
    git("reset", "--hard", old, cwd=repo)
    return newer


def test_compress_squashes_onto_origin_when_local_base_is_stale(
    repo: Path, tmp_path: Path, commit
):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    newer = _stale_local_main(repo, commit)
    for name in ("one", "two"):
        (wt / f"{name}.txt").write_text("x\n")
        git("add", "-A", cwd=wt)
        git("commit", "-m", f"own {name}", cwd=wt)
    git("merge", "--no-edit", "origin/main", cwd=wt)

    compress_branch(root=wt, message="squashed")

    # The squash sits on the current origin/main and holds only the branch's work.
    assert git("rev-parse", "HEAD^", cwd=wt) == newer
    changed = git("diff", "--name-only", "HEAD^", "HEAD", cwd=wt).splitlines()
    assert sorted(changed) == ["one.txt", "two.txt"]


def test_compress_squashes_onto_local_base_when_it_is_ahead_of_origin(
    repo: Path, tmp_path: Path, commit
):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    commit("unpushed.txt", "x\n", "local-only main work")  # origin/main is behind
    local = git("rev-parse", "main", cwd=repo)
    git("merge", "--no-edit", "main", cwd=wt)
    for name in ("one", "two"):
        (wt / f"{name}.txt").write_text("x\n")
        git("add", "-A", cwd=wt)
        git("commit", "-m", f"own {name}", cwd=wt)

    compress_branch(root=wt, message="squashed", push=False)

    assert git("rev-parse", "HEAD^", cwd=wt) == local
    changed = git("diff", "--name-only", "HEAD^", "HEAD", cwd=wt).splitlines()
    assert sorted(changed) == ["one.txt", "two.txt"]


def test_compress_noop_on_single_commit(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    (wt / "only.txt").write_text("x\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "only commit", cwd=wt)
    assert _commits_since("main", cwd=wt) == 1
    before = git("rev-parse", "HEAD", cwd=wt)

    compress_branch(root=wt, message=None)  # single commit -> nothing to do

    assert git("rev-parse", "HEAD", cwd=wt) == before  # unchanged


def test_compress_errors_on_untracked_branch(repo: Path):
    git("checkout", "-b", "loose", cwd=repo)
    with pytest.raises(typer.Exit):
        compress_branch(root=repo, message="x")


# --- diff-parent ------------------------------------------------------------


def test_diff_parent_command_uses_lineage_parent(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)

    cmd = diff_parent_command(root=wt)

    # Defaults to difftastic via diff.external.
    assert cmd == ["git", "-c", "diff.external=difft", "diff", "main...HEAD"]


def test_diff_parent_command_compares_with_origin_when_local_base_is_stale(
    repo: Path, tmp_path: Path, commit
):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)
    _stale_local_main(repo, commit)

    cmd = diff_parent_command(root=wt)

    assert cmd == ["git", "-c", "diff.external=difft", "diff", "origin/main...HEAD"]


def test_diff_parent_command_passes_extra_args(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)

    cmd = diff_parent_command(root=wt, extra_args=["--stat"])

    assert cmd == [
        "git",
        "-c",
        "diff.external=difft",
        "diff",
        "main...HEAD",
        "--stat",
    ]


def test_diff_parent_command_line_uses_plain_diff(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=wt)

    cmd = diff_parent_command(root=wt, line=True)

    assert cmd == ["git", "diff", "main...HEAD"]


# --- set-parent -------------------------------------------------------------


def test_set_parent_repoints_lineage(repo: Path, tmp_path: Path):
    api = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api)
    tests = tmp_path / "wt-tests"
    create_stacked_branch("api_tests", root=api, wt_path=tests)
    # api_tests -> api -> main; repoint api_tests straight onto main.
    assert lineage.get_parent("devon/api_tests", root=tests) == "devon/api"

    set_branch_parent(root=tests, new_parent="main")

    assert lineage.get_parent("devon/api_tests", root=tests) == "main"


def test_set_parent_rejects_cycle(repo: Path, tmp_path: Path):
    api = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api)
    tests = tmp_path / "wt-tests"
    create_stacked_branch("api_tests", root=api, wt_path=tests)
    # Standing on devon/api, try to set its parent to its own descendant.
    with pytest.raises(typer.Exit):
        set_branch_parent(root=api, new_parent="devon/api_tests")


def test_set_parent_rejects_missing_branch(repo: Path, tmp_path: Path):
    api = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api)
    with pytest.raises(typer.Exit):
        set_branch_parent(root=api, new_parent="does/not/exist")
