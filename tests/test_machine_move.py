"""`tt machine export` / `import` between two throwaway homes with different
user names, a fake `op`, and real git repos."""

import json
import os
import sqlite3
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolbelt.git.stack.store import RepoIdentity, StackStore
from toolbelt.machine_move.archive_layout import MachineMoveError
from toolbelt.machine_move.cli import machine_typer
from toolbelt.machine_move.export_archive import ExportRequest, export_machine
from toolbelt.machine_move.import_archive import ImportRequest, import_machine
from toolbelt.machine_move.path_prefixes import PrefixMap, claude_project_dirname

runner = CliRunner()

FAKE_OP = """#!/bin/sh
# Stores documents as files in $FAKE_OP_DIR.
[ -f "$FAKE_OP_DIR/signed-out" ] && [ "$1" = whoami ] && exit 1
case "$1 $2" in
  "whoami "*) echo user ;;
  "item list") echo "[]" ;;
  "document create") cp "$3" "$FAKE_OP_DIR/doc.zip" ;;
  "document get") while [ $# -gt 0 ]; do [ "$1" = --out-file ] && out="$2"; shift; done
                  cp "$FAKE_OP_DIR/doc.zip" "$out" ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "f").write_text("x")
    git(path, "add", "f")
    git(path, "commit", "-q", "-m", "init")
    return path


class Laptop:
    def __init__(self, root: Path, user: str) -> None:
        self.home = root / "Users" / user
        self.workdir = self.home / "git"
        self.state = self.home / ".toolbelt"
        self.home.mkdir(parents=True)

    def export_request(self, **kwargs) -> ExportRequest:
        return ExportRequest(
            home=self.home,
            workdir=self.workdir,
            db_path=self.state / "stacks.db",
            title="t",
            to_file=kwargs.pop("to_file", None),
            **kwargs,
        )


@pytest.fixture
def old(tmp_path: Path) -> Laptop:
    laptop = Laptop(tmp_path, "olduser")
    for name in ("dotfiles", "dotfiles-private", "toolbelt"):
        make_repo(laptop.workdir / name)
    secrets = laptop.workdir / "dotfiles" / "secrets"
    secrets.mkdir()
    (laptop.workdir / "dotfiles" / ".gitignore").write_text("secrets\n")
    (secrets / "machine.sh").write_text("export IS_PERSONAL=false\n")
    (secrets / "toolbelt.sh").write_text(
        f"export TOKEN_FILE={laptop.home}/tok\nexport OTHER={laptop.home}er/x\n"
    )
    ssh = laptop.home / ".ssh"
    ssh.mkdir(mode=0o700)
    (ssh / "work_key").write_text("PRIVATE")
    (ssh / "work_key.pub").write_text("PUBLIC")
    (ssh / "unrelated_key").write_text("NOT CARRIED")
    (laptop.home / ".gitconfig").write_text(
        "[user]\n\tsigningkey = ~/.ssh/work_key.pub\n"
    )
    memory = (
        laptop.home
        / ".claude/projects"
        / claude_project_dirname(str(laptop.workdir))
        / "memory"
    )
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text(f"repo at {laptop.workdir}/app\n")
    transcript = (
        laptop.home
        / ".claude/projects"
        / claude_project_dirname(str(laptop.workdir))
        / "t.jsonl"
    )
    transcript.write_text("transcript")
    (laptop.home / ".zsh_history").write_text("ls\n")
    return laptop


def seed_db(laptop: Laptop, origin: Path) -> Path:
    app = laptop.workdir / "app"
    subprocess.run(["git", "clone", "-q", str(origin), str(app)], check=True)
    git(app, "checkout", "-q", "-b", "devon/feature")
    (app / "g").write_text("g")
    git(app, "add", "g")
    git(app, "commit", "-q", "-m", "feature")
    git(app, "push", "-q", "origin", "devon/feature")
    git(app, "checkout", "-q", "main")
    wt = laptop.workdir / "wt" / "app" / "feature"
    git(app, "worktree", "add", str(wt), "devon/feature")
    store = StackStore(laptop.state / "stacks.db")
    repo_id = store.ensure_repo(RepoIdentity(app / ".git", app, str(origin)))
    store.set_parent(repo_id, "devon/feature", "main")
    store.close()
    conn = sqlite3.connect(laptop.state / "stacks.db")
    conn.execute(
        "INSERT INTO transcript_files VALUES (?, 5)", (str(laptop.home / "t.jsonl"),)
    )
    conn.execute(
        "INSERT INTO claim_spans (holder, holder_kind, claim_kind, target, first_seen, last_seen) "
        "VALUES ('h', 'agent', 'worktree', ?, 'a', 'b')",
        (str(wt),),
    )
    conn.execute(
        "INSERT INTO usage_calls VALUES ('m', 's', NULL, 'model', 'now', ?, 1, 1, 1, 1, 1, '{}', 0.5)",
        (str(wt),),
    )
    conn.commit()
    conn.close()
    return wt


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    path = tmp_path / "origin.git"
    seed = make_repo(tmp_path / "seed")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(path)], check=True)
    return path


def test_round_trip_into_a_home_with_another_user_name(
    tmp_path: Path, old: Laptop, origin: Path
) -> None:
    wt = seed_db(old, origin)
    archive = tmp_path / "out" / "archive.zip"
    report = export_machine(old.export_request(to_file=archive), vault=None)  # type: ignore[arg-type]
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    carried = {m.member for m in report.members}
    assert "workdir/dotfiles/secrets/machine.sh" in carried
    assert "home/.ssh/work_key" in carried and "home/.ssh/work_key.pub" in carried
    assert "home/.ssh/unrelated_key" not in carried
    assert not any("t.jsonl" in m or ".zsh_history" in m for m in carried)
    with zipfile.ZipFile(archive) as zf:
        assert "NOT CARRIED" not in b"".join(zf.read(n) for n in zf.namelist()).decode(
            errors="ignore"
        )

    new = Laptop(tmp_path, "newuser")
    for name in ("dotfiles", "dotfiles-private", "toolbelt"):
        make_repo(new.workdir / name)
    request = ImportRequest(
        archive=archive, home=new.home, workdir=new.workdir, state_dir=new.state
    )
    result = import_machine(request)
    assert result.follow_ups == []

    toolbelt_sh = (new.workdir / "dotfiles/secrets/toolbelt.sh").read_text()
    assert f"TOKEN_FILE={new.home}/tok" in toolbelt_sh
    assert f"{old.home}er/x" in toolbelt_sh  # a longer name is not the old home
    assert stat.S_IMODE((new.home / ".ssh/work_key").stat().st_mode) == 0o600
    assert stat.S_IMODE((new.home / ".ssh").stat().st_mode) == 0o700
    memory = (
        new.home
        / ".claude/projects"
        / claude_project_dirname(str(new.workdir))
        / "memory/MEMORY.md"
    )
    assert memory.read_text() == f"repo at {new.workdir}/app\n"

    store = StackStore(new.state / "stacks.db")
    try:
        [repo] = store.repos()
        assert repo.path == new.workdir / "app"
        assert repo.git_common_dir == new.workdir / "app/.git"
        assert store.get_parent(repo.id, "devon/feature") == "main"
    finally:
        store.close()
    conn = sqlite3.connect(new.state / "stacks.db")
    assert conn.execute("SELECT COUNT(*) FROM transcript_files").fetchone() == (0,)
    assert conn.execute("SELECT target FROM claim_spans").fetchone() == (
        str(new.workdir / "wt/app/feature"),
    )
    assert conn.execute("SELECT cwd FROM usage_calls").fetchone() == (
        str(new.workdir / "wt/app/feature"),
    )
    conn.close()
    assert old.home.name not in json.dumps(
        [
            str(r)
            for r in sqlite3.connect(new.state / "stacks.db").execute(
                "SELECT path, git_common_dir FROM repos"
            )
        ]
    )

    # The tracked repo was cloned and its worktree recreated at the new path.
    assert (new.workdir / "app/.git").is_dir()
    assert (
        git(new.workdir / "wt/app/feature", "branch", "--show-current").strip()
        == "devon/feature"
    )
    assert wt.exists()

    again = import_machine(request)
    assert again.follow_ups == []
    assert not [
        line
        for line in again.done
        if line.startswith(("restored", "cloned", "worktree"))
    ]


def test_import_keeps_a_database_that_has_data_unless_told(
    tmp_path: Path, old: Laptop, origin: Path
) -> None:
    seed_db(old, origin)
    archive = tmp_path / "a.zip"
    export_machine(old.export_request(to_file=archive), vault=None)  # type: ignore[arg-type]
    new = Laptop(tmp_path, "newuser")
    for name in ("dotfiles", "dotfiles-private", "toolbelt"):
        make_repo(new.workdir / name)
    store = StackStore(new.state / "stacks.db")
    store.ensure_repo(RepoIdentity(new.workdir / "x/.git", new.workdir / "x", None))
    store.close()
    request = ImportRequest(
        archive=archive,
        home=new.home,
        workdir=new.workdir,
        state_dir=new.state,
        clone_missing=False,
    )
    kept = import_machine(request)
    assert any(line.startswith("kept") for line in kept.done)
    replaced = import_machine(ImportRequest(**{**request.__dict__, "replace_db": True}))
    assert any(line.startswith("backed up") for line in replaced.done)
    store = StackStore(new.state / "stacks.db")
    assert [r.name for r in store.repos()] == ["app"]
    store.close()
    assert any((new.home / ".toolbelt/machine-import-backups").rglob("stacks.db"))


def test_import_backs_up_a_differing_file(tmp_path: Path, old: Laptop) -> None:
    archive = tmp_path / "a.zip"
    export_machine(old.export_request(to_file=archive), vault=None)  # type: ignore[arg-type]
    new = Laptop(tmp_path, "newuser")
    make_repo(new.workdir / "dotfiles")
    (new.workdir / "dotfiles/secrets").mkdir()
    (new.workdir / "dotfiles/secrets/machine.sh").write_text("local\n")
    result = import_machine(
        ImportRequest(
            archive=archive, home=new.home, workdir=new.workdir, state_dir=new.state
        )
    )
    assert (
        new.workdir / "dotfiles/secrets/machine.sh"
    ).read_text() == "export IS_PERSONAL=false\n"
    [backup] = (new.home / ".toolbelt/machine-import-backups").rglob("machine.sh")
    assert backup.read_text() == "local\n"
    # dotfiles-private and toolbelt are not cloned here: said, not silently skipped.
    assert result.done


def test_export_refuses_a_personal_machine_sh_without_force(
    tmp_path: Path, old: Laptop
) -> None:
    (old.workdir / "dotfiles/secrets/machine.sh").write_text(
        "export IS_PERSONAL=true\n"
    )
    with pytest.raises(MachineMoveError, match="IS_PERSONAL=true"):
        export_machine(old.export_request(to_file=tmp_path / "a.zip"), vault=None)  # type: ignore[arg-type]
    forced = export_machine(
        old.export_request(to_file=tmp_path / "a.zip", force=True), vault=None
    )  # type: ignore[arg-type]
    assert any("IS_PERSONAL" in w for w in forced.warnings)


def test_export_refuses_while_a_run_is_active_or_a_decision_pending(
    tmp_path: Path, old: Laptop, origin: Path
) -> None:
    seed_db(old, origin)
    conn = sqlite3.connect(old.state / "stacks.db")
    conn.execute(
        "INSERT INTO agent_runs (run_id, task_id, repo_id, branch, trigger, head_sha, model, state, started_at) "
        "VALUES ('r', 't', 1, 'b', 'x', 'h', 'm', 'running', 'now')"
    )
    conn.execute(
        "INSERT INTO decisions (id, task_id, repo_id, branch, proposer, title, points, status, created_at) "
        "VALUES ('d', 't', 1, 'b', 'p', 'T', '[]', 'pending', 'now')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(MachineMoveError) as error:
        export_machine(old.export_request(to_file=tmp_path / "a.zip"), vault=None)  # type: ignore[arg-type]
    assert "1 autonomous run(s) running" in str(error.value)
    assert "1 decision(s) pending" in str(error.value)
    archive = tmp_path / "forced.zip"
    export_machine(old.export_request(to_file=archive, force=True), vault=None)  # type: ignore[arg-type]
    with zipfile.ZipFile(archive) as zf:
        zf.extract("db/stacks.db", tmp_path / "x")
    state = (
        sqlite3.connect(tmp_path / "x/db/stacks.db")
        .execute("SELECT state FROM agent_runs")
        .fetchone()
    )
    assert state == ("abandoned",)


def test_export_lists_uncommitted_unpushed_and_stashed_work(
    tmp_path: Path, old: Laptop, origin: Path
) -> None:
    wt = seed_db(old, origin)
    (wt / "dirty").write_text("d")
    (wt / "g").write_text("changed")
    git(wt, "stash", "push", "-q", "-m", "mine")
    (wt / "g").write_text("changed again")
    (wt / "g2").write_text("x")
    git(wt, "add", "g2")
    git(wt, "commit", "-q", "-m", "local only")
    report = export_machine(old.export_request(to_file=tmp_path / "a.zip"), vault=None)  # type: ignore[arg-type]
    problems = [item.problem for item in report.in_flight]
    assert any("uncommitted" in p for p in problems)
    assert "1 commit(s) not pushed" in problems
    assert any(p.startswith("stash") and "mine" in p for p in problems)


def test_ignored_files_not_carried_are_listed(tmp_path: Path, old: Laptop) -> None:
    (old.workdir / "toolbelt/.gitignore").write_text("scratch.txt\n.venv\n")
    (old.workdir / "toolbelt/scratch.txt").write_text("x")
    (old.workdir / "toolbelt/.venv").mkdir()
    report = export_machine(old.export_request(to_file=tmp_path / "a.zip"), vault=None)  # type: ignore[arg-type]
    assert report.ignored_not_carried == ["toolbelt/scratch.txt"]


def test_no_ssh_keys_and_history_flags(tmp_path: Path, old: Laptop) -> None:
    plain = export_machine(
        old.export_request(to_file=tmp_path / "a.zip", ssh_keys=None), vault=None
    )  # type: ignore[arg-type]
    assert not any(m.path.startswith(".ssh") for m in plain.members)
    with_history = export_machine(
        old.export_request(to_file=tmp_path / "b.zip", include_history=True), vault=None
    )  # type: ignore[arg-type]
    assert "home/.zsh_history" in {m.member for m in with_history.members}


def test_cli_round_trip_through_a_fake_op(
    tmp_path: Path, old: Laptop, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "op"
    fake.write_text(FAKE_OP)
    fake.chmod(0o755)
    store_dir = tmp_path / "fake-op"
    store_dir.mkdir()
    monkeypatch.setenv("TOOLBELT_OP", str(fake))
    monkeypatch.setenv("FAKE_OP_DIR", str(store_dir))
    base = [
        "--home",
        str(old.home),
        "--workdir",
        str(old.workdir),
        "--state-dir",
        str(old.state),
    ]

    (store_dir / "signed-out").write_text("")
    signed_out = runner.invoke(machine_typer, ["export", *base])
    assert signed_out.exit_code == 1
    assert "not signed in" in signed_out.output
    (store_dir / "signed-out").unlink()

    exported = runner.invoke(machine_typer, ["export", *base])
    assert exported.exit_code == 0, exported.output
    assert (store_dir / "doc.zip").is_file()
    assert "PRIVATE" not in exported.output

    new = Laptop(tmp_path, "newuser")
    for name in ("dotfiles", "dotfiles-private", "toolbelt"):
        make_repo(new.workdir / name)
    imported = runner.invoke(
        machine_typer,
        [
            "import",
            "--home",
            str(new.home),
            "--workdir",
            str(new.workdir),
            "--state-dir",
            str(new.state),
        ],
    )
    assert imported.exit_code == 0, imported.output
    assert (new.home / ".ssh/work_key").read_text() == "PRIVATE"


def test_missing_op_binary_is_a_clear_error(
    tmp_path: Path, old: Laptop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOOLBELT_OP", str(tmp_path / "no-such-op"))
    result = runner.invoke(
        machine_typer,
        ["export", "--home", str(old.home), "--workdir", str(old.workdir)],
    )
    assert result.exit_code == 1
    assert "brew install 1password-cli" in result.output


def test_prefix_map() -> None:
    prefixes = PrefixMap.build(
        old_home="/Users/old",
        old_workdir="/Users/old/git",
        new_home=Path("/h/new"),
        new_workdir=Path("/h/new/git"),
    )
    assert prefixes.path("/Users/old/git/x") == "/h/new/git/x"
    assert prefixes.path("/Users/old") == "/h/new"
    assert prefixes.path("/Users/older/x") == "/Users/older/x"
    assert prefixes.text("a /Users/old/b /Users/older") == "a /h/new/b /Users/older"
    assert prefixes.claude_project("-Users-old-git-wt-x") == "-h-new-git-wt-x"
    assert prefixes.claude_project("-Users-older") == "-Users-older"
    assert os.sep == "/"
