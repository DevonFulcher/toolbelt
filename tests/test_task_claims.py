"""Task claims (src/toolbelt/task_claims, docs/task-claims.md): reading the
claims directory, revocations, resolving claims to tracked branches, and the
`tt task` commands. The clock, the directories and the worktree lister are
injected; nothing here reads the real ones."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolbelt.git.stack.store import RepoIdentity, StackStore
from toolbelt.task_claims import operations
from toolbelt.task_claims.atomic_file import write_atomically
from toolbelt.task_claims.cli import TaskEnvironment, build_task_typer
from toolbelt.task_claims.reading import STALE_AFTER, read_claims
from toolbelt.task_claims.resolution import (
    TrackedTask,
    resolve_claim,
    tracked_tasks,
)
from toolbelt.task_claims.revocations import record_revocations

NOW = datetime(2026, 10, 12, 20, 0, 0, tzinfo=UTC)


def _z(time: datetime) -> str:
    return time.isoformat().replace("+00:00", "Z")


def write_holder(
    claims_dir: Path,
    holder: str,
    *,
    state: str = "working",
    updated_ago: float = 5,
    ttl: int = 180,
    claims: list[dict] | None = None,
    kind: str = "agent",
    link: str | None = None,
    **extra,
) -> Path:
    """A claim file written the way the contract says (independent of the
    code under test)."""
    claims_dir.mkdir(parents=True, exist_ok=True)
    updated = NOW - timedelta(seconds=updated_ago)
    document = {
        "version": 1,
        "holder": holder,
        "kind": kind,
        "session_id": None if kind == "human" else holder.split(".")[0],
        "agent_id": None,
        "link": link,
        "state": state,
        "started_at": _z(updated - timedelta(minutes=10)),
        "updated_at": _z(updated),
        "ttl_seconds": ttl,
        "claims": claims or [],
        **extra,
    }
    path = claims_dir / f"{holder}.json"
    path.write_text(json.dumps(document))
    return path


def ref_claim(ref: str, ago: float = 20) -> dict:
    return {"kind": "ref", "ref": ref, "touched_at": _z(NOW - timedelta(seconds=ago))}


def worktree_claim(path: str | Path, ago: float = 20) -> dict:
    return {
        "kind": "worktree",
        "path": str(path),
        "touched_at": _z(NOW - timedelta(seconds=ago)),
    }


# ---- reading ----------------------------------------------------------------------


def test_a_holder_is_live_until_updated_at_plus_ttl(tmp_path: Path):
    claims = tmp_path / "claims"
    write_holder(claims, "fresh", updated_ago=179, ttl=180)
    write_holder(claims, "exact", updated_ago=180, ttl=180)
    write_holder(claims, "idle", state="idle", updated_ago=1000, ttl=1800)
    write_holder(claims, "blocked", state="blocked", updated_ago=1, ttl=1800)

    reading = read_claims(claims, NOW)

    assert {h.holder: h.status for h in reading.holders} == {
        "blocked": "blocked",
        "exact": "expired",
        "fresh": "working",
        "idle": "idle",
    }
    assert [h.holder for h in reading.live] == ["blocked", "fresh", "idle"]
    assert [h.holder for h in reading.expired] == ["exact"]


def test_a_working_holder_that_stopped_heartbeating_is_stale_but_live(tmp_path: Path):
    claims = tmp_path / "claims"
    write_holder(claims, "hung", updated_ago=STALE_AFTER.total_seconds() + 1)
    write_holder(claims, "ok", updated_ago=10)
    write_holder(claims, "quiet", state="idle", updated_ago=600, ttl=1800)

    by_id = {h.holder: h for h in read_claims(claims, NOW).holders}

    assert by_id["hung"].live and by_id["hung"].stale
    assert not by_id["ok"].stale
    assert not by_id["quiet"].stale  # idle holders do not heartbeat


def test_unreadable_partial_and_unknown_version_files_are_skipped_and_listed(
    tmp_path: Path,
):
    claims = tmp_path / "claims"
    write_holder(claims, "good", claims=[ref_claim("PLAT-1")])
    (claims / "partial.json").write_text('{"version": 1, "holder": "par')
    write_holder(claims, "future", version=2)
    write_holder(claims, "misnamed").rename(claims / "other-name.json")
    (claims / "notes.txt").write_text("not a claim")
    (claims / ".good.json.123.tmp").write_text("{")

    reading = read_claims(claims, NOW)

    assert [h.holder for h in reading.holders] == ["good"]
    skipped = {s.name: s.reason for s in reading.skipped}
    assert set(skipped) == {"partial.json", "future.json", "other-name.json"}
    assert "unsupported version 2" in skipped["future.json"]
    assert "does not match" in skipped["other-name.json"]


def test_a_missing_directory_reads_as_nothing(tmp_path: Path):
    reading = read_claims(tmp_path / "nope", NOW)

    assert reading.holders == () and reading.skipped == ()


def test_a_claim_touched_at_or_before_a_revocation_is_dropped_a_later_one_stands(
    tmp_path: Path,
):
    claims = tmp_path / "claims"
    write_holder(
        claims,
        "s1",
        claims=[
            ref_claim("old", ago=60),
            ref_claim("same-instant", ago=30),
            ref_claim("retouched", ago=5),
            worktree_claim("/wt/gone", ago=60),
            worktree_claim("/wt/kept", ago=60),
        ],
    )
    (claims / "revoked").mkdir()
    at = _z(NOW - timedelta(seconds=30))
    (claims / "revoked" / "s1.json").write_text(
        json.dumps(
            {
                "version": 1,
                "holder": "s1",
                "revoked": [
                    {"kind": "ref", "ref": "old", "at": at, "by": "tt task release"},
                    {"kind": "ref", "ref": "same-instant", "at": at, "by": "x"},
                    {"kind": "ref", "ref": "retouched", "at": at, "by": "x"},
                    {"kind": "worktree", "path": "/wt/gone", "at": at, "by": "x"},
                ],
            }
        )
    )

    (holder,) = read_claims(claims, NOW).holders

    assert [c.ref if c.kind == "ref" else c.path for c in holder.claims] == [
        "retouched",
        "/wt/kept",
    ]
    assert len(holder.revoked) == 3


def test_a_broken_revocation_file_leaves_the_claims_standing_and_is_listed(
    tmp_path: Path,
):
    claims = tmp_path / "claims"
    write_holder(claims, "s1", claims=[ref_claim("PLAT-1")])
    (claims / "revoked").mkdir()
    (claims / "revoked" / "s1.json").write_text("{nope")

    reading = read_claims(claims, NOW)

    assert len(reading.holders[0].claims) == 1
    assert [s.name for s in reading.skipped] == ["revoked/s1.json"]


def test_the_atomic_writer_leaves_no_temp_file_and_replaces_whole(tmp_path: Path):
    target = tmp_path / "claims" / "human.devon.json"

    write_atomically(target, "one")
    write_atomically(target, "two")

    assert target.read_text() == "two"
    assert [p.name for p in target.parent.iterdir()] == ["human.devon.json"]


# ---- resolving ----------------------------------------------------------------------


def _store_with_tasks(tmp_path: Path) -> tuple[StackStore, int]:
    store = StackStore(tmp_path / "stacks.db")
    repo_path = tmp_path / "repos" / "app"
    repo_path.mkdir(parents=True)
    repo_id = store.ensure_repo(
        RepoIdentity(git_common_dir=repo_path / ".git", path=repo_path, remote_url=None)
    )
    store.set_parent(repo_id, "devon/models", "main")
    store.set_parent(repo_id, "devon/api", "devon/models")
    store.set_links(repo_id, "devon/models", task="PLAT-412", task_tracker="jira")
    store.set_links(repo_id, "devon/api", task="PLAT-412", task_tracker="jira")
    return store, repo_id


def _lister(tmp_path: Path):
    calls: list[Path] = []
    worktrees = {
        tmp_path / "wt" / "app" / "models": "devon/models",
        tmp_path / "wt" / "app" / "api": "devon/api",
    }

    def list_worktrees(root: Path) -> dict[Path, str]:
        calls.append(root)
        return worktrees

    return list_worktrees, calls


def test_claims_resolve_by_worktree_branch_ticket_and_task_id(tmp_path: Path):
    store, _ = _store_with_tasks(tmp_path)
    lister, calls = _lister(tmp_path)
    tasks = tracked_tasks(store, lister)
    by_branch = {t.branch: t for t in tasks}
    now = NOW

    def resolve(claim: dict):
        from toolbelt.task_claims.operations import make_claim

        subject = Path(claim["path"]) if claim["kind"] == "worktree" else claim["ref"]
        return [t.branch for t in resolve_claim(make_claim(subject, now=now), tasks)]

    assert len(calls) == 1  # git is asked once per repo, not once per claim
    assert resolve(worktree_claim(tmp_path / "wt" / "app" / "api")) == ["devon/api"]
    assert resolve(ref_claim("devon/models")) == ["devon/models"]
    assert resolve(ref_claim("plat-412")) == ["devon/models", "devon/api"]
    assert resolve(ref_claim(by_branch["devon/api"].task_id)) == ["devon/api"]
    assert resolve(ref_claim("nothing")) == []
    assert resolve(worktree_claim(tmp_path / "elsewhere")) == []


def test_a_worktree_claim_follows_a_symlinked_path(tmp_path: Path):
    store, _ = _store_with_tasks(tmp_path)
    real = tmp_path / "real-wt"
    real.mkdir()
    link = tmp_path / "link-wt"
    link.symlink_to(real)
    tasks = tracked_tasks(store, lambda _root: {real: "devon/api"})

    from toolbelt.task_claims.operations import make_claim

    found = resolve_claim(make_claim(link, now=NOW), tasks)

    assert [t.branch for t in found] == ["devon/api"]


def test_tracked_tasks_skips_the_worktree_listing_of_a_missing_checkout(
    tmp_path: Path,
):
    store, _ = _store_with_tasks(tmp_path)
    (tmp_path / "repos" / "app").rmdir()
    lister, calls = _lister(tmp_path)

    tasks = tracked_tasks(store, lister)

    assert calls == []
    assert all(t.worktree is None for t in tasks)


# ---- operations -------------------------------------------------------------------------


def test_durations_parse_to_seconds_minutes_hours_and_days():
    assert operations.parse_duration("90") == timedelta(seconds=90)
    assert operations.parse_duration("30m") == timedelta(minutes=30)
    assert operations.parse_duration("8h") == timedelta(hours=8)
    assert operations.parse_duration("2d") == timedelta(days=2)
    with pytest.raises(ValueError):
        operations.parse_duration("soon")
    with pytest.raises(ValueError):
        operations.parse_duration("0")


def test_holder_names_are_limited_to_filename_safe_characters():
    assert operations.human_holder("devon") == "human.devon"
    with pytest.raises(ValueError):
        operations.human_holder("a/b")


def test_human_claims_are_merged_refreshed_and_keep_their_start(tmp_path: Path):
    claims = tmp_path / "claims"
    later = NOW + timedelta(hours=1)

    operations.write_human_claims(
        claims,
        name="devon",
        claims=[operations.make_claim("PLAT-1", now=NOW)],
        ttl=timedelta(hours=8),
        now=NOW,
    )
    operations.write_human_claims(
        claims,
        name="devon",
        claims=[
            operations.make_claim("PLAT-1", now=later),
            operations.make_claim("PLAT-2", now=later),
        ],
        ttl=timedelta(minutes=30),
        now=later,
    )

    (holder,) = read_claims(claims, later).holders
    assert holder.holder == "human.devon"
    assert holder.file.kind == "human" and holder.status == "working"
    assert holder.file.started_at == NOW
    assert holder.file.ttl_seconds == 1800
    assert [c.ref for c in holder.claims if c.kind == "ref"] == ["PLAT-1", "PLAT-2"]
    assert all(c.touched_at == later for c in holder.claims)


def _selector_for(tasks: list[TrackedTask], *branches: str):
    targets = {t.key for t in tasks if t.branch in branches}
    return operations.select_by_resolution(targets, set(branches), tasks)


def _tasks(tmp_path: Path) -> list[TrackedTask]:
    store, _ = _store_with_tasks(tmp_path)
    lister, _ = _lister(tmp_path)
    return tracked_tasks(store, lister)


def test_release_removes_human_claims_and_deletes_an_emptied_file(tmp_path: Path):
    claims = tmp_path / "claims"
    tasks = _tasks(tmp_path)
    write_holder(
        claims,
        "human.devon",
        kind="human",
        ttl=28800,
        claims=[ref_claim("devon/api"), ref_claim("devon/models")],
    )

    reading = read_claims(claims, NOW)
    first = operations.release_claims(
        claims, reading, select=_selector_for(tasks, "devon/api"), force=False, now=NOW
    )

    assert [(r.holder, r.how) for r in first.released] == [("human.devon", "removed")]
    (holder,) = read_claims(claims, NOW).holders
    assert [c.ref for c in holder.claims if c.kind == "ref"] == ["devon/models"]

    operations.release_claims(
        claims,
        read_claims(claims, NOW),
        select=_selector_for(tasks, "devon/models"),
        force=False,
        now=NOW,
    )
    assert not (claims / "human.devon.json").exists()


def test_release_revokes_an_agents_claims_without_touching_its_file(tmp_path: Path):
    claims = tmp_path / "claims"
    tasks = _tasks(tmp_path)
    path = write_holder(
        claims,
        "s1",
        state="idle",
        ttl=1800,
        claims=[ref_claim("devon/api"), ref_claim("devon/models")],
    )
    before = path.read_text()

    outcome = operations.release_claims(
        claims,
        read_claims(claims, NOW),
        select=_selector_for(tasks, "devon/api"),
        force=False,
        now=NOW,
    )

    assert [(r.holder, r.how) for r in outcome.released] == [("s1", "revoked")]
    assert path.read_text() == before
    revoked = json.loads((claims / "revoked" / "s1.json").read_text())
    assert revoked == {
        "version": 1,
        "holder": "s1",
        "revoked": [
            {
                "kind": "ref",
                "ref": "devon/api",
                "at": _z(NOW),
                "by": "tt task release",
            }
        ],
    }
    (holder,) = read_claims(claims, NOW).holders
    assert [c.ref for c in holder.claims if c.kind == "ref"] == ["devon/models"]


def test_a_working_heartbeating_agent_needs_force_a_stale_one_does_not(
    tmp_path: Path,
):
    claims = tmp_path / "claims"
    tasks = _tasks(tmp_path)
    write_holder(claims, "alive", updated_ago=5, claims=[ref_claim("devon/api")])
    write_holder(
        claims,
        "hung",
        updated_ago=STALE_AFTER.total_seconds() + 5,
        claims=[ref_claim("devon/api")],
    )

    plain = operations.release_claims(
        claims,
        read_claims(claims, NOW),
        select=_selector_for(tasks, "devon/api"),
        force=False,
        now=NOW,
    )

    assert [r.holder for r in plain.released] == ["hung"]
    assert [r.holder for r in plain.refused] == ["alive"]
    assert not (claims / "revoked" / "alive.json").exists()

    forced = operations.release_claims(
        claims,
        read_claims(claims, NOW),
        select=_selector_for(tasks, "devon/api"),
        force=True,
        now=NOW,
    )
    assert "alive" in [r.holder for r in forced.released]


def test_a_revocation_replaces_an_earlier_entry_for_the_same_claim(tmp_path: Path):
    claims = tmp_path / "claims"
    first = operations.make_claim("devon/api", now=NOW)
    other = operations.make_claim("devon/models", now=NOW)

    record_revocations(claims, "s1", [first, other], at=NOW, by="a")
    record_revocations(claims, "s1", [first], at=NOW + timedelta(minutes=1), by="b")

    entries = json.loads((claims / "revoked" / "s1.json").read_text())["revoked"]
    assert {(e["ref"], e["by"]) for e in entries} == {
        ("devon/models", "a"),
        ("devon/api", "b"),
    }


def test_gc_deletes_expired_files_and_orphans_and_keeps_the_rest(tmp_path: Path):
    claims = tmp_path / "claims"
    write_holder(claims, "dead", updated_ago=500, claims=[ref_claim("x")])
    write_holder(claims, "alive", claims=[ref_claim("y")])
    (claims / "partial.json").write_text("{")
    revoked = claims / "revoked"
    revoked.mkdir()
    for holder in ("dead", "alive", "never-existed", "partial"):
        (revoked / f"{holder}.json").write_text("{}")
    old_temp = claims / ".dead.json.1.tmp"
    old_temp.write_text("{")
    os.utime(old_temp, ((NOW - timedelta(hours=2)).timestamp(),) * 2)
    new_temp = claims / ".alive.json.2.tmp"
    new_temp.write_text("{")
    os.utime(new_temp, (NOW.timestamp(),) * 2)

    outcome = operations.collect_garbage(claims, read_claims(claims, NOW), NOW)

    assert outcome.claim_files == ["dead.json"]
    assert sorted(outcome.revocation_files) == [
        "revoked/dead.json",
        "revoked/never-existed.json",
    ]
    assert outcome.temp_files == [".dead.json.1.tmp"]
    assert sorted(p.name for p in claims.iterdir()) == [
        ".alive.json.2.tmp",
        "alive.json",
        "partial.json",
        "revoked",
    ]
    assert sorted(p.name for p in revoked.iterdir()) == ["alive.json", "partial.json"]


def test_gc_keeps_a_file_that_revived_after_the_scan(tmp_path: Path):
    claims = tmp_path / "claims"
    write_holder(claims, "s1", updated_ago=500)
    stale_reading = read_claims(claims, NOW)
    write_holder(claims, "s1", updated_ago=1)  # the holder wrote again

    outcome = operations.collect_garbage(claims, stale_reading, NOW)

    assert outcome.claim_files == []
    assert (claims / "s1.json").exists()


# ---- the commands ---------------------------------------------------------------------------


@pytest.fixture
def task_cli(tmp_path: Path):
    store, _ = _store_with_tasks(tmp_path)
    store.close()
    lister, _ = _lister(tmp_path)
    claims = tmp_path / "claims"
    clock = {"now": NOW}
    environment = TaskEnvironment(
        now=lambda: clock["now"],
        claims_dir=lambda: claims,
        db_path=lambda: tmp_path / "stacks.db",
        list_worktrees=lister,
        user=lambda: "devon",
    )
    app = build_task_typer(lambda: environment)
    runner = CliRunner()

    def invoke(*args: str):
        return runner.invoke(app, list(args))

    invoke.claims = claims  # type: ignore[attr-defined]
    invoke.clock = clock  # type: ignore[attr-defined]
    return invoke


def test_claim_writes_a_human_file_that_status_shows(task_cli):
    result = task_cli("claim", "PLAT-412", "--ttl", "2h")

    assert result.exit_code == 0, result.output
    assert "claimed app:devon/models" in result.output
    assert "claimed app:devon/api" in result.output
    file = json.loads((task_cli.claims / "human.devon.json").read_text())
    assert file["kind"] == "human" and file["state"] == "working"
    assert file["ttl_seconds"] == 7200
    assert file["claims"] == [{"kind": "ref", "ref": "PLAT-412", "touched_at": _z(NOW)}]

    status = task_cli("status")
    assert "human.devon" in status.output
    assert "app:devon/api" in status.output


def test_claim_of_something_untracked_fails_and_writes_nothing(task_cli):
    result = task_cli("claim", "devon/models", "nonsense")

    assert result.exit_code == 1
    assert not (task_cli.claims / "human.devon.json").exists()


def test_release_by_ref_then_by_holder_and_all(task_cli):
    task_cli("claim", "devon/api")
    write_holder(
        task_cli.claims,
        "agent1",
        state="idle",
        ttl=1800,
        claims=[ref_claim("devon/models")],
    )

    one = task_cli("release", "devon/api")
    assert one.exit_code == 0, one.output
    assert not (task_cli.claims / "human.devon.json").exists()

    agent = task_cli("release", "--holder", "agent1")
    assert "revoked agent1" in agent.output
    assert (task_cli.claims / "revoked" / "agent1.json").exists()

    none = task_cli("release", "--all")
    assert none.exit_code == 1 and "nothing to release" in none.output


def test_release_needs_exactly_one_selector(task_cli):
    assert task_cli("release").exit_code != 0
    assert task_cli("release", "devon/api", "--all").exit_code != 0


def test_release_of_a_working_agent_is_refused_until_forced(task_cli):
    write_holder(task_cli.claims, "agent1", claims=[ref_claim("devon/api")])

    refused = task_cli("release", "devon/api")
    forced = task_cli("release", "devon/api", "--force")

    assert refused.exit_code == 1 and "--force" in refused.output
    assert forced.exit_code == 0
    assert (task_cli.claims / "revoked" / "agent1.json").exists()


def test_status_json_lists_resolution_staleness_and_ignored_files(task_cli):
    write_holder(
        task_cli.claims,
        "agent1",
        updated_ago=120,
        link="claude://claude.ai/epitaxy/abc",
        claims=[ref_claim("devon/api"), ref_claim("ghost")],
    )
    write_holder(task_cli.claims, "old", updated_ago=900, claims=[ref_claim("x")])
    (task_cli.claims / "broken.json").write_text("{")

    document = json.loads(task_cli("status", "--json").output)

    by_id = {h["holder"]: h for h in document["holders"]}
    assert by_id["agent1"]["stale"] is True and by_id["agent1"]["live"] is True
    assert by_id["old"]["live"] is False
    resolved = {c["ref"]: c["resolved"] for c in by_id["agent1"]["claims"]}
    assert [t["branch"] for t in resolved["devon/api"]] == ["devon/api"]
    assert resolved["ghost"] == []
    assert [u["file"] for u in document["unreadable"]] == ["broken.json"]


def test_status_text_hides_expired_and_unresolved_until_verbose(task_cli):
    write_holder(
        task_cli.claims,
        "agent1",
        claims=[ref_claim("devon/api"), ref_claim("ghost")],
    )
    write_holder(task_cli.claims, "old", updated_ago=900, claims=[ref_claim("x")])
    (task_cli.claims / "broken.json").write_text("{")

    plain = task_cli("status").output
    verbose = task_cli("status", "--verbose").output

    assert "app:devon/api" in plain
    assert "old" not in plain.split("expired")[0]
    assert "ghost" not in plain and "1 claim(s) match no tracked task" in plain
    assert "ghost" in verbose and "(unresolved)" in verbose
    assert "old" in verbose and "EXPIRED" in verbose
    assert "broken.json" in verbose


def test_gc_command_reports_what_it_deleted(task_cli):
    write_holder(task_cli.claims, "old", updated_ago=900)

    result = task_cli("gc")

    assert "deleted old.json" in result.output
    assert not (task_cli.claims / "old.json").exists()
    assert "nothing to delete" in task_cli("gc").output


def test_task_group_is_registered_without_importing_the_claims_machinery():
    import subprocess
    import sys

    code = (
        "import sys, toolbelt.cli;"
        "print(any(m.startswith('toolbelt.task_claims.') and m != "
        "'toolbelt.task_claims.cli' for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "False"
