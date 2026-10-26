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
from toolbelt.machine_move.in_flight import UnpushedBranch
from toolbelt.machine_move.path_prefixes import PrefixMap, claude_project_dirname
from toolbelt.machine_move.push_branches import push_unpushed
from toolbelt.machine_move.service import LaunchdService

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
    stashes = [item for item in report.in_flight if "stash" in item.problem]
    assert [(s.problem, s.informational) for s in stashes] == [
        ("1 stash(es), not carried", True)
    ]


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


def fake_git(origin_head: str = "main", fail: set[str] | None = None):
    """A runner that records pushes instead of running git."""
    pushes: list[list[str]] = []

    def run(repo: Path, args: list[str]) -> tuple[int, str]:
        if args[0] == "symbolic-ref":
            return 0, f"origin/{origin_head}"
        pushes.append(args)
        if fail and args[-1] in fail:
            return 1, "remote: ...\n ! [rejected] (non-fast-forward)"
        return 0, ""

    return run, pushes


REPO = Path("/r/app")


class TestPushRules:
    def push(self, branches, *, states=None, always=frozenset(), **git_kwargs):
        run, pushes = fake_git(**git_kwargs)
        results = push_unpushed(
            branches, pr_states=states or {}, always_push=set(always), git=run
        )
        return results, pushes

    def test_a_branch_with_an_open_pr_and_no_upstream_is_pushed_with_one(self):
        results, pushes = self.push(
            [UnpushedBranch(REPO, "devon/a", False)],
            states={(str(REPO), "devon/a"): "open"},
        )

        assert [(r.outcome) for r in results] == ["pushed"]
        assert pushes == [["push", "-u", "origin", "devon/a"]]

    def test_a_draft_pr_counts_as_open(self):
        results, _ = self.push(
            [UnpushedBranch(REPO, "devon/a", False)],
            states={(str(REPO), "devon/a"): "draft"},
        )

        assert results[0].outcome == "pushed"

    def test_a_branch_with_an_upstream_gets_a_plain_push(self):
        results, pushes = self.push(
            [UnpushedBranch(REPO, "devon/a", True)],
            states={(str(REPO), "devon/a"): "open"},
        )

        assert results[0].outcome == "pushed"
        assert pushes == [["push", "origin", "devon/a"]]

    @pytest.mark.parametrize("state", ["merged", "closed"])
    def test_a_branch_whose_pr_is_finished_is_stale_whatever_it_has(self, state):
        results, pushes = self.push(
            [
                UnpushedBranch(REPO, "devon/a", True),
                UnpushedBranch(REPO, "devon/b", False),
            ],
            states={(str(REPO), "devon/a"): state, (str(REPO), "devon/b"): state},
        )

        assert [r.outcome for r in results] == ["skipped", "skipped"]
        assert state in results[0].detail
        assert pushes == []

    def test_a_branch_with_no_pr_and_no_upstream_is_left_for_you(self):
        results, pushes = self.push([UnpushedBranch(REPO, "devon/a", False)])

        assert results[0].outcome == "skipped"
        assert "push it yourself" in results[0].detail
        assert pushes == []

    def test_the_default_branch_of_a_work_repo_is_not_pushed(self):
        results, pushes = self.push([UnpushedBranch(REPO, "main", True)])

        assert results[0].outcome == "skipped" and pushes == []

    def test_the_dotfiles_repos_are_pushed_on_their_default_branch(self):
        results, pushes = self.push(
            [UnpushedBranch(Path("/g/dotfiles"), "main", True)],
            always={Path("/g/dotfiles")},
        )

        assert results[0].outcome == "pushed"
        assert pushes == [["push", "origin", "main"]]

    def test_a_rejected_push_is_reported_and_never_forced(self):
        results, pushes = self.push(
            [UnpushedBranch(REPO, "devon/a", True)],
            states={(str(REPO), "devon/a"): "open"},
            fail={"devon/a"},
        )

        assert results[0].outcome == "failed" and "rejected" in results[0].detail
        assert "--force" not in pushes[0] and "-f" not in pushes[0]


def save_pr_state(laptop: Laptop, branch: str, state: str) -> None:
    conn = sqlite3.connect(laptop.state / "stacks.db")
    conn.execute(
        "INSERT INTO pr_lookups (repo_id, branch, fetched_at, full_at, record, schema) "
        "SELECT id, ?, 'a', 'b', ?, 1 FROM repos",
        (branch, json.dumps({"status": {"pr": state}})),
    )
    conn.commit()
    conn.close()


class TestExportPushes:
    def commit_locally(self, wt: Path, name: str) -> None:
        (wt / name).write_text(name)
        git(wt, "add", name)
        git(wt, "commit", "-q", "-m", name)

    def test_the_export_pushes_a_live_branch_and_no_longer_lists_it(
        self, tmp_path: Path, old: Laptop, origin: Path
    ) -> None:
        wt = seed_db(old, origin)
        self.commit_locally(wt, "local-only")
        save_pr_state(old, "devon/feature", "open")

        report = export_machine(
            old.export_request(to_file=tmp_path / "a.zip", push_branches=True),
            vault=None,  # type: ignore[arg-type]
        )

        assert [(p.branch, p.outcome) for p in report.pushed if p.repo == "app"] == [
            ("devon/feature", "pushed")
        ]
        assert not any(
            "not pushed" in i.problem and i.repo == "app" for i in report.in_flight
        )
        remote = git(origin, "log", "--format=%s", "devon/feature")
        assert "local-only" in remote

    def test_a_stale_branch_is_reported_and_stays_unpushed(
        self, tmp_path: Path, old: Laptop, origin: Path
    ) -> None:
        wt = seed_db(old, origin)
        self.commit_locally(wt, "local-only")
        save_pr_state(old, "devon/feature", "merged")

        report = export_machine(
            old.export_request(to_file=tmp_path / "a.zip", push_branches=True),
            vault=None,  # type: ignore[arg-type]
        )

        (result,) = [p for p in report.pushed if p.repo == "app"]
        assert result.outcome == "skipped" and "merged" in result.detail
        assert "local-only" not in git(origin, "log", "--format=%s", "devon/feature")

    def test_nothing_is_pushed_unless_asked(
        self, tmp_path: Path, old: Laptop, origin: Path
    ) -> None:
        wt = seed_db(old, origin)
        self.commit_locally(wt, "local-only")
        save_pr_state(old, "devon/feature", "open")

        report = export_machine(
            old.export_request(to_file=tmp_path / "a.zip"),
            vault=None,  # type: ignore[arg-type]
        )

        assert report.pushed == []


class FakeService:
    def __init__(self, laptop: Laptop | None = None) -> None:
        self.calls: list[str] = []
        self._laptop = laptop

    def stop(self) -> str:
        self.calls.append("stop")
        if self._laptop is not None:  # something the service wrote before it died
            conn = sqlite3.connect(self._laptop.state / "stacks.db")
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('written_by_the_service', '1')"
            )
            conn.commit()
            conn.close()
        return "stopped the service"

    def start(self) -> None:
        self.calls.append("start")


class TestExportStopsTheService:
    def test_it_is_stopped_before_the_db_is_copied_and_left_stopped(
        self, tmp_path: Path, old: Laptop, origin: Path
    ) -> None:
        seed_db(old, origin)
        service = FakeService(old)

        report = export_machine(
            old.export_request(to_file=tmp_path / "a.zip", stop_service=True),
            vault=None,  # type: ignore[arg-type]
            service=service,
        )

        assert service.calls == ["stop"]
        assert report.service_note == "stopped the service"
        with zipfile.ZipFile(tmp_path / "a.zip") as archive:
            archive.extract("db/stacks.db", tmp_path / "x")
        rows = (
            sqlite3.connect(tmp_path / "x" / "db" / "stacks.db")
            .execute("SELECT value FROM settings WHERE key = 'written_by_the_service'")
            .fetchall()
        )
        assert rows == [("1",)]  # the snapshot is taken after it stopped

    def test_it_is_left_alone_unless_asked(
        self, tmp_path: Path, old: Laptop, origin: Path
    ) -> None:
        seed_db(old, origin)
        service = FakeService()

        export_machine(
            old.export_request(to_file=tmp_path / "a.zip"),
            vault=None,  # type: ignore[arg-type]
            service=service,
        )

        assert service.calls == []

    def test_a_failed_export_starts_it_again(
        self, tmp_path: Path, old: Laptop, origin: Path
    ) -> None:
        seed_db(old, origin)
        service = FakeService()

        class BrokenVault:
            def ensure_signed_in(self) -> None: ...

            def store(self, path: Path, *, title: str) -> str:
                raise MachineMoveError("1Password said no")

        with pytest.raises(MachineMoveError):
            export_machine(
                old.export_request(stop_service=True),
                vault=BrokenVault(),  # type: ignore[arg-type]
                service=service,
            )

        assert service.calls == ["stop", "start"]

    def test_a_refused_export_never_stops_it(self, tmp_path: Path, old: Laptop) -> None:
        (old.workdir / "dotfiles/secrets/machine.sh").write_text(
            "export IS_PERSONAL=true\n"
        )
        service = FakeService()

        with pytest.raises(MachineMoveError):
            export_machine(
                old.export_request(to_file=tmp_path / "a.zip", stop_service=True),
                vault=None,  # type: ignore[arg-type]
                service=service,
            )

        assert service.calls == []


class TestLaunchdService:
    def service(self, monkeypatch, *, loaded: bool, forwarders: list[list[int]]):
        calls: list[tuple[str, ...]] = []
        killed: list[int] = []
        service = LaunchdService(home=Path("/h"), label="l")
        monkeypatch.setattr(
            service,
            "_launchctl",
            lambda *args: (
                calls.append(args)
                or subprocess.CompletedProcess(
                    args, 0 if loaded or args[0] != "print" else 1
                )
            ),
        )
        sequence = iter(forwarders)
        monkeypatch.setattr(
            LaunchdService, "_forwarders", staticmethod(lambda: next(sequence, []))
        )
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(pid))
        monkeypatch.setattr("time.sleep", lambda s: None)
        return service, calls, killed

    def test_it_boots_the_service_out_and_waits_for_its_forwarders(self, monkeypatch):
        service, calls, killed = self.service(
            monkeypatch, loaded=True, forwarders=[[11], [11], []]
        )

        note = service.stop()

        assert any(c[0] == "bootout" for c in calls)
        assert killed == [] and "stopped" in note

    def test_forwarders_that_outlive_it_are_ended(self, monkeypatch):
        service, _calls, killed = self.service(
            monkeypatch, loaded=True, forwarders=[[11]] * 40
        )
        monkeypatch.setattr(
            "time.monotonic", iter([0.0, 0.0, 99.0, 99.0, 99.0, 99.0]).__next__
        )

        note = service.stop()

        assert killed == [11] and "ended 1" in note

    def test_a_service_that_is_not_loaded_is_left_alone(self, monkeypatch):
        service, calls, _ = self.service(monkeypatch, loaded=False, forwarders=[])

        note = service.stop()

        assert all(c[0] != "bootout" for c in calls) and "was not running" in note


class TestSetUpRepos:
    def checkout(self, path: Path, *files: str) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        for name in files:
            (path / name).write_text("")
        return path

    def run_setup(self, checkouts, fail=()):
        from toolbelt.machine_move.setup_repos import set_up

        ran: list[tuple[str, str]] = []

        def runner(checkout: Path, command: list[str]) -> tuple[int, str]:
            ran.append((checkout.name, " ".join(command)))
            return (1, "boom\nlast line") if " ".join(command) in fail else (0, "")

        return set_up(checkouts, runner), ran

    def test_each_checkout_gets_what_it_has_files_for(self, tmp_path: Path) -> None:
        both = self.checkout(tmp_path / "both", ".tool-versions", "uv.lock")
        tools = self.checkout(tmp_path / "tools", ".tool-versions")
        neither = self.checkout(tmp_path / "neither")

        results, ran = self.run_setup([both, tools, neither])

        assert ran == [
            ("both", "asdf install"),
            ("both", "uv sync"),
            ("tools", "asdf install"),
        ]
        assert all(r.ok for r in results)

    def test_a_failure_is_reported_and_the_rest_still_run(self, tmp_path: Path) -> None:
        first = self.checkout(tmp_path / "first", ".tool-versions", "uv.lock")
        second = self.checkout(tmp_path / "second", "uv.lock")

        results, ran = self.run_setup([first, second], fail={"asdf install"})

        assert ran == [
            ("first", "asdf install"),
            ("first", "uv sync"),
            ("second", "uv sync"),
        ]
        assert [(r.command, r.ok, r.detail) for r in results[:1]] == [
            ("asdf install", False, "last line")
        ]

    def test_a_checkout_that_is_not_there_is_skipped(self, tmp_path: Path) -> None:
        results, ran = self.run_setup([tmp_path / "missing"])

        assert results == [] and ran == []
