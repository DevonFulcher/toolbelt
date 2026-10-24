"""Task claims (src/toolbelt/task_claims, docs/task-claims.md): reading the
claims directory, revocations, resolving claims to tracked branches, and the
`tt task` commands. The clock, the directories and the worktree lister are
injected; nothing here reads the real ones."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolbelt.git.stack.store import RepoIdentity, StackStore
from toolbelt.task_claims import operations
from toolbelt.task_claims.atomic_file import write_atomically
from toolbelt.task_claims.cli import TaskEnvironment, build_task_typer
from toolbelt.task_claims.process_liveness import (
    ProcessLiveness,
    parse_lstart,
    ps_start_times,
)
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
        "started_at": _z(NOW - timedelta(minutes=10)),
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
    write_holder(claims, "future", version=3)
    write_holder(claims, "misnamed").rename(claims / "other-name.json")
    (claims / "notes.txt").write_text("not a claim")
    (claims / ".good.json.123.tmp").write_text("{")

    reading = read_claims(claims, NOW)

    assert [h.holder for h in reading.holders] == ["good"]
    skipped = {s.name: s.reason for s in reading.skipped}
    assert set(skipped) == {"partial.json", "future.json", "other-name.json"}
    assert "unsupported version 3" in skipped["future.json"]
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
        pause=lambda: None,
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


# ---- amendments from the writer (docs/task-claims.md "Writer behaviour") ----------------


def test_a_tombstone_with_ttl_zero_is_dead_and_collected_by_gc(tmp_path: Path):
    claims = tmp_path / "claims"
    write_holder(claims, "s1", state="idle", ttl=0, updated_ago=0, claims=[])

    reading = read_claims(claims, NOW)

    assert [h.status for h in reading.holders] == ["expired"]
    assert reading.live == ()
    outcome = operations.collect_garbage(claims, reading, NOW)
    assert outcome.claim_files == ["s1.json"]


def test_an_empty_or_half_written_file_is_skipped_not_an_error(tmp_path: Path):
    claims = tmp_path / "claims"
    claims.mkdir()
    (claims / "empty.json").write_text("")
    write_holder(claims, "ok")

    reading = read_claims(claims, NOW)

    assert [h.holder for h in reading.holders] == ["ok"]
    assert [s.name for s in reading.skipped] == ["empty.json"]


def test_settled_reading_lists_only_files_unreadable_twice(tmp_path: Path):
    from toolbelt.task_claims.reading import read_claims_settled

    claims = tmp_path / "claims"
    claims.mkdir()
    (claims / "mid-write.json").write_text("{")
    (claims / "broken.json").write_text("{")

    def finish_the_write():
        write_holder(claims, "mid-write", claims=[ref_claim("PLAT-1")])

    reading = read_claims_settled(claims, NOW, finish_the_write)

    assert [h.holder for h in reading.holders] == ["mid-write"]
    assert [s.name for s in reading.skipped] == ["broken.json"]


SCHEMA = json.loads(
    (Path(__file__).parent / "claim_schema" / "claim.schema.json").read_text()
)


def _validate(value, schema: dict, path: str = "$") -> list[str]:
    """The subset of JSON Schema the writer's schema uses (no jsonschema
    dependency): type, const, enum, pattern, minimum, minLength, required,
    additionalProperties, oneOf, $ref into $defs."""
    import re

    if "$ref" in schema:
        return _validate(value, SCHEMA["$defs"][schema["$ref"].split("/")[-1]], path)
    errors: list[str] = []
    if "oneOf" in schema:
        matching = [s for s in schema["oneOf"] if not _validate(value, s, path)]
        return (
            [] if len(matching) == 1 else [f"{path}: matches {len(matching)} of oneOf"]
        )
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: not {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not one of {schema['enum']}")
    types = {
        "string": str,
        "integer": int,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    if "type" in schema:
        allowed = (
            schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        )
        if not any(isinstance(value, types[t]) for t in allowed):
            return [*errors, f"{path}: wrong type"]
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: too short")
    if isinstance(value, int) and value < schema.get("minimum", value):
        errors.append(f"{path}: below minimum")
    if isinstance(value, dict):
        errors += [
            f"{path}.{k}: missing" for k in schema.get("required", []) if k not in value
        ]
        for key, item in value.items():
            if key in schema.get("properties", {}):
                errors += _validate(item, schema["properties"][key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: not allowed")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += _validate(item, schema["items"], f"{path}[{index}]")
    return errors


def test_the_test_helpers_write_files_that_satisfy_the_writers_schema(tmp_path: Path):
    path = write_holder(
        tmp_path,
        "sess.sub1",
        link="claude://claude.ai/epitaxy/abc",
        claims=[ref_claim("PLAT-1"), worktree_claim("/Users/devon/git/wt/app/x")],
    )

    assert _validate(json.loads(path.read_text()), SCHEMA) == []
    assert _validate({"version": 1}, SCHEMA) != []  # the validator does reject


def test_human_files_written_by_tt_satisfy_the_schema_apart_from_their_kind(
    tmp_path: Path,
):
    claims = tmp_path / "claims"
    operations.write_human_claims(
        claims,
        name="devon",
        claims=[
            operations.make_claim("PLAT-1", now=NOW),
            operations.make_claim(Path("/wt/app/x"), now=NOW),
        ],
        ttl=timedelta(hours=8),
        now=NOW,
    )

    document = json.loads((claims / "human.devon.json").read_text())

    assert document["kind"] == "human"
    assert _validate({**document, "kind": "agent"}, SCHEMA) == []


def test_gc_grace_keeps_a_file_that_expired_a_moment_ago(tmp_path: Path):
    claims = tmp_path / "claims"
    write_holder(claims, "recent", updated_ago=185)  # expired 5 s ago
    write_holder(claims, "old", updated_ago=200)  # expired 20 s ago
    write_holder(claims, "tomb", ttl=0, updated_ago=15, state="idle")

    outcome = operations.collect_garbage(
        claims, read_claims(claims, NOW), NOW, grace=timedelta(seconds=10)
    )

    assert sorted(outcome.claim_files) == ["old.json", "tomb.json"]
    assert (claims / "recent.json").exists()


def test_a_v2_file_may_be_an_autonomous_holder_with_a_run_id(tmp_path: Path):
    from toolbelt.task_claims.claim_file import parse_claim_file

    path = write_holder(
        tmp_path / "claims",
        "auto.run1",
        kind="autonomous",
        version=2,
        run_id="run1",
        claims=[ref_claim("devon/a")],
    )

    parsed = parse_claim_file(path.read_text(), expected_holder="auto.run1")

    assert (parsed.version, parsed.kind, parsed.run_id) == (2, "autonomous", "run1")


@pytest.mark.parametrize(
    ("kind", "version"), [("autonomous", 1), ("agent", 2), ("human", 2)]
)
def test_the_autonomous_kind_is_exactly_contract_v2(tmp_path: Path, kind, version):
    from toolbelt.task_claims.claim_file import UnreadableFile, parse_claim_file

    path = write_holder(tmp_path / "claims", "x", kind=kind, version=version)

    with pytest.raises(UnreadableFile):
        parse_claim_file(path.read_text(), expected_holder="x")


# ---- the writer's process (pid + process_started_at) ------------------------------------

STARTED = NOW - timedelta(hours=1)


class FakeProcesses:
    """The processes of a fake machine: ``running`` maps pid to its start
    time. Counts the ``ps`` calls (``ps_calls``, one list of pids each)."""

    def __init__(self, running: dict[int, datetime]) -> None:
        self.running = running
        self.ps_calls: list[list[int]] = []
        self.clock = 0.0

    def signal(self, pid: int, sig: int) -> None:
        assert sig == 0
        if pid not in self.running:
            raise ProcessLookupError(pid)

    def start_times(self, pids: list[int]) -> dict[int, datetime]:
        self.ps_calls.append(list(pids))
        return {p: self.running[p] for p in pids if p in self.running}

    def liveness(self) -> ProcessLiveness:
        return ProcessLiveness(
            signal=self.signal,
            start_times=self.start_times,
            monotonic=lambda: self.clock,
        )


def write_process_holder(claims: Path, holder: str, pid: int, **kwargs) -> Path:
    return write_holder(
        claims, holder, pid=pid, process_started_at=_z(STARTED), **kwargs
    )


def test_a_live_pid_with_the_recorded_start_time_is_live(tmp_path: Path):
    claims = tmp_path / "claims"
    write_process_holder(claims, "alive", 100)
    machine = FakeProcesses({100: STARTED})

    (holder,) = read_claims(claims, NOW, machine.liveness()).holders

    assert (holder.status, holder.process_gone, holder.live) == ("working", False, True)


def test_a_dead_pid_is_dead_before_its_ttl(tmp_path: Path):
    claims = tmp_path / "claims"
    write_process_holder(claims, "dead", 100, updated_ago=5, ttl=180)
    machine = FakeProcesses({})

    (holder,) = read_claims(claims, NOW, machine.liveness()).holders

    assert holder.file.expires_at > NOW  # the ttl alone would call it live
    assert (holder.status, holder.process_gone, holder.live) == ("expired", True, False)


def test_a_reused_pid_with_another_start_time_is_dead(tmp_path: Path):
    claims = tmp_path / "claims"
    write_process_holder(claims, "reused", 100)
    machine = FakeProcesses({100: STARTED + timedelta(minutes=5)})

    (holder,) = read_claims(claims, NOW, machine.liveness()).holders

    assert (holder.status, holder.process_gone) == ("expired", True)


def test_files_without_the_process_fields_behave_as_before(tmp_path: Path):
    claims = tmp_path / "claims"
    write_holder(claims, "plain", updated_ago=5)
    write_holder(claims, "half", updated_ago=5, pid=100)  # one field alone: ignored
    write_holder(claims, "human.devon", kind="human", ttl=3600)
    write_process_holder(claims, "late", 100, updated_ago=500)  # past ttl anyway
    machine = FakeProcesses({})

    reading = read_claims(claims, NOW, machine.liveness())

    assert {h.holder: (h.status, h.process_gone) for h in reading.holders} == {
        "half": ("working", False),
        "human.devon": ("working", False),
        "late": ("expired", False),
        "plain": ("working", False),
    }
    assert machine.ps_calls == []


def test_an_unverifiable_start_time_leaves_the_ttl_to_decide(tmp_path: Path):
    claims = tmp_path / "claims"
    write_process_holder(claims, "w", 100)
    liveness = ProcessLiveness(
        signal=lambda pid, sig: None,
        start_times=lambda pids: None,  # ps could not be run
    )

    (holder,) = read_claims(claims, NOW, liveness).holders

    assert (holder.status, holder.process_gone) == ("working", False)


def test_start_times_are_looked_up_in_one_call_and_cached(tmp_path: Path):
    claims = tmp_path / "claims"
    for pid in (100, 101, 102):
        write_process_holder(claims, f"h{pid}", pid)
    machine = FakeProcesses({100: STARTED, 101: STARTED, 102: STARTED})
    liveness = machine.liveness()

    for _ in range(5):
        read_claims(claims, NOW, liveness)

    assert machine.ps_calls == [[100, 101, 102]]

    machine.clock += 31  # the cache entries age out
    read_claims(claims, NOW, liveness)

    assert len(machine.ps_calls) == 2


def test_a_pid_that_died_is_forgotten_so_a_reused_pid_is_looked_up_afresh(
    tmp_path: Path,
):
    claims = tmp_path / "claims"
    write_process_holder(claims, "w", 100)
    machine = FakeProcesses({100: STARTED})
    liveness = machine.liveness()
    assert read_claims(claims, NOW, liveness).holders[0].live

    del machine.running[100]
    assert not read_claims(claims, NOW, liveness).holders[0].live
    machine.running[100] = STARTED + timedelta(days=1)  # the pid, reused

    assert not read_claims(claims, NOW, liveness).holders[0].live
    assert len(machine.ps_calls) == 2


def test_gc_deletes_a_dead_writers_file_at_once_even_with_a_grace(tmp_path: Path):
    claims = tmp_path / "claims"
    write_process_holder(claims, "dead", 100)
    write_process_holder(claims, "alive", 101)
    machine = FakeProcesses({101: STARTED})
    liveness = machine.liveness()

    outcome = operations.collect_garbage(
        claims,
        read_claims(claims, NOW, liveness),
        NOW,
        grace=timedelta(seconds=10),
        liveness=liveness,
    )

    assert outcome.claim_files == ["dead.json"]
    assert (claims / "alive.json").exists()


def test_status_json_reports_the_process_and_why_a_holder_is_dead(tmp_path: Path):
    from toolbelt.task_claims.status_report import status_document, status_text

    claims = tmp_path / "claims"
    write_process_holder(claims, "dead", 100)
    reading = read_claims(claims, NOW, FakeProcesses({}).liveness())

    document = status_document(reading, [], NOW)

    (holder,) = document["holders"]
    assert (holder["pid"], holder["process_gone"], holder["live"]) == (100, True, False)
    assert holder["process_started_at"] == _z(STARTED)
    assert "its process is gone" in status_text(document, verbose=True)


def test_the_process_fields_satisfy_the_writers_schema(tmp_path: Path):
    path = write_process_holder(tmp_path / "claims", "s1", 100)

    assert _validate(json.loads(path.read_text()), SCHEMA) == []
    bad = json.loads(path.read_text()) | {"pid": 0}
    assert _validate(bad, SCHEMA) != []


def test_lstart_parses_as_utc_with_padded_days():
    assert parse_lstart("Wed Oct 21 14:23:05 2026") == datetime(
        2026, 10, 21, 14, 23, 5, tzinfo=UTC
    )
    assert parse_lstart("Thu Oct  1 04:03:02 2026") == datetime(
        2026, 10, 1, 4, 3, 2, tzinfo=UTC
    )
    assert parse_lstart("garbage") is None


def test_the_real_ps_sees_this_process_and_a_dead_one_is_gone():
    liveness = ProcessLiveness()
    started = (ps_start_times([os.getpid()]) or {}).get(os.getpid())
    assert started is not None
    assert liveness.is_alive(os.getpid(), started)
    assert not liveness.is_alive(os.getpid(), started - timedelta(days=1))

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not liveness.is_alive(child.pid, started)


def test_a_claim_file_may_say_what_the_holder_is_running():
    from toolbelt.task_claims.claim_file import parse_claim_file

    text = json.dumps(
        {
            "version": 1,
            "holder": "s",
            "kind": "agent",
            "session_id": "s",
            "agent_id": None,
            "link": None,
            "state": "working",
            "started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:30Z",
            "ttl_seconds": 180,
            "activity": {
                "tool": "Bash",
                "detail": "pnpm test",
                "since": "2026-01-01T00:00:20Z",
                "open": True,
            },
            "claims": [],
        }
    )

    parsed = parse_claim_file(text, expected_holder="s")

    assert parsed.activity is not None
    assert (parsed.activity.tool, parsed.activity.detail, parsed.activity.open) == (
        "Bash",
        "pnpm test",
        True,
    )
    # A reader that predates the field ignores it; a file without it has none.
    assert (
        parse_claim_file(
            text.replace('"activity"', '"x"'), expected_holder="s"
        ).activity
        is None
    )
