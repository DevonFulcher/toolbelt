"""Task claims in `tt ui`: the claims feed (polling, expiry, the one-bad-read
grace, when git is asked) and the whole path to the page: the snapshot's
"With an agent" next action, and the `claims` events that re-derive a branch
and the branches stacked on it.

The directory, the tracked-branch lister, the wall clock and sleeping are
injected; nothing reads the real claims directory or runs git."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from test_task_claims import NOW, ref_claim, worktree_claim, write_holder
from test_ui import FakeStatuses, branches, open_status, refresh, seed_repo, snap
from toolbelt.git.stack.status import ReviewState
from toolbelt.task_claims.resolution import TrackedTask
from toolbelt.ui import api_models
from toolbelt.ui.app import create_app
from toolbelt.ui.claims_feed import ClaimsFeed
from toolbelt.ui.live_status import EventHub


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def tracked(*names: str, repo_id: int = 1) -> list[TrackedTask]:
    return [
        TrackedTask(
            repo_id=repo_id,
            repo_name="api",
            branch=name,
            task_id=f"id-{index}",
            task_ref=None,
            worktree=Path(f"/wt/api/{name.removeprefix('devon/')}"),
        )
        for index, name in enumerate(names)
    ]


class Feed:
    def __init__(self, tmp_path: Path, names: tuple[str, ...] = ("devon/a", "devon/b")):
        self.dir = tmp_path / "claims"
        self.clock = Clock()
        self.changes: list[set[tuple[int, str]]] = []
        self.task_calls = 0
        self.names = names

        def tasks() -> list[TrackedTask]:
            self.task_calls += 1
            return tracked(*self.names)

        self.feed = ClaimsFeed(
            directory=lambda: self.dir,
            tasks=tasks,
            hub=EventHub(),
            on_change=self.changes.append,
            wall_clock=self.clock,
            resolve_ttl=15,
        )


# ---- the feed -----------------------------------------------------------------------------


def test_a_working_holder_claims_its_branches_and_expires_on_its_own(tmp_path):
    f = Feed(tmp_path)
    write_holder(f.dir, "s1", claims=[ref_claim("devon/a")], link="claude://x/1")

    changed = f.feed.poll()

    assert changed == {(1, "devon/a")}
    assert f.feed.working(1, "devon/a") and not f.feed.working(1, "devon/b")
    (holder,) = f.feed.holders(1, "devon/a")
    assert (holder.holder, holder.state, holder.link) == (
        "s1",
        "working",
        "claude://x/1",
    )

    f.clock.now += timedelta(seconds=300)  # past its 180 s ttl, no file change

    assert f.feed.poll() == {(1, "devon/a")}
    assert not f.feed.working(1, "devon/a")
    assert f.feed.holders(1, "devon/a") == ()


def test_heartbeats_and_unrelated_polls_report_nothing(tmp_path):
    f = Feed(tmp_path)
    write_holder(f.dir, "s1", claims=[ref_claim("devon/a")], updated_ago=5)
    f.feed.poll()
    f.changes.clear()

    f.clock.now += timedelta(seconds=10)
    write_holder(f.dir, "s1", claims=[ref_claim("devon/a")], updated_ago=0)

    assert f.feed.poll() == set()
    assert f.changes == []


def test_idle_and_blocked_holders_are_listed_but_do_not_claim(tmp_path):
    f = Feed(tmp_path)
    write_holder(f.dir, "s1", state="idle", ttl=1800, claims=[ref_claim("devon/a")])
    write_holder(f.dir, "s2", state="blocked", ttl=1800, claims=[ref_claim("devon/b")])

    f.feed.poll()

    assert not f.feed.working(1, "devon/a") and not f.feed.working(1, "devon/b")
    assert [h.state for h in f.feed.holders(1, "devon/a")] == ["idle"]
    assert [h.state for h in f.feed.holders(1, "devon/b")] == ["blocked"]


def test_a_tombstone_and_a_revoked_claim_do_not_count(tmp_path):
    f = Feed(tmp_path)
    write_holder(f.dir, "gone", state="idle", ttl=0, updated_ago=0, claims=[])
    write_holder(
        f.dir, "s1", claims=[ref_claim("devon/a", ago=60), ref_claim("devon/b", ago=5)]
    )
    f.feed.poll()
    assert f.feed.working(1, "devon/a")

    from toolbelt.task_claims.operations import make_claim
    from toolbelt.task_claims.revocations import record_revocations

    record_revocations(
        f.dir,
        "s1",
        [make_claim("devon/a", now=NOW)],
        at=NOW - timedelta(seconds=30),
        by="x",
    )

    assert f.feed.poll() == {(1, "devon/a")}
    assert not f.feed.working(1, "devon/a") and f.feed.working(1, "devon/b")


def test_one_unreadable_read_keeps_the_holder_two_drop_it(tmp_path):
    f = Feed(tmp_path)
    path = write_holder(f.dir, "s1", claims=[ref_claim("devon/a")])
    f.feed.poll()
    good = path.read_text()

    path.write_text("")  # mid-overwrite
    assert f.feed.poll() == set()
    assert f.feed.working(1, "devon/a")

    assert f.feed.poll() == {(1, "devon/a")}  # still unreadable: dropped
    assert not f.feed.working(1, "devon/a")

    path.write_text(good)
    assert f.feed.poll() == {(1, "devon/a")}
    assert f.feed.working(1, "devon/a")


def test_a_kept_holder_still_expires_by_its_ttl(tmp_path):
    f = Feed(tmp_path)
    path = write_holder(f.dir, "s1", claims=[ref_claim("devon/a")])
    f.feed.poll()
    path.write_text("{")
    f.clock.now += timedelta(seconds=400)

    assert f.feed.poll() == {(1, "devon/a")}
    assert not f.feed.working(1, "devon/a")


def test_git_is_asked_only_when_the_claims_change_or_the_resolution_is_old(tmp_path):
    f = Feed(tmp_path)

    f.feed.poll()
    assert f.task_calls == 0  # nobody holds anything: nothing to resolve

    write_holder(f.dir, "s1", claims=[ref_claim("devon/a")], updated_ago=1)
    f.feed.poll()
    assert f.task_calls == 1

    f.clock.now += timedelta(seconds=5)
    write_holder(f.dir, "s1", claims=[ref_claim("devon/a")], updated_ago=0)  # heartbeat
    f.feed.poll()
    assert f.task_calls == 1

    write_holder(f.dir, "s1", claims=[ref_claim("devon/b")], updated_ago=0)
    f.feed.poll()
    assert f.task_calls == 2

    f.clock.now += timedelta(seconds=20)
    write_holder(f.dir, "s1", claims=[ref_claim("devon/b")], updated_ago=0)
    f.feed.poll()
    assert f.task_calls == 3  # a branch may have been added meanwhile


def test_worktree_claims_resolve_and_two_holders_on_one_branch_both_show(tmp_path):
    f = Feed(tmp_path)
    write_holder(f.dir, "s1", claims=[worktree_claim("/wt/api/a")])
    write_holder(f.dir, "s1.sub", claims=[ref_claim("devon/a")], state="idle", ttl=1800)

    f.feed.poll()

    assert [h.holder for h in f.feed.holders(1, "devon/a")] == ["s1", "s1.sub"]


def test_the_loop_waits_for_a_browser_and_polls_while_one_is_connected(tmp_path):
    f = Feed(tmp_path)
    hub = f.feed._hub  # noqa: SLF001
    polls: list[int] = []
    real = f.feed.poll
    f.feed.poll = lambda: polls.append(1) or real()  # type: ignore[method-assign]

    async def scenario():
        ticks = asyncio.Event()

        async def sleep(_seconds: float) -> None:
            await ticks.wait()
            ticks.clear()

        f.feed._sleep = sleep  # noqa: SLF001
        task = asyncio.create_task(f.feed.run())
        for _ in range(10):
            await asyncio.sleep(0)
        assert polls == []  # nobody is watching
        queue = hub.subscribe()
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(polls) == 1
        ticks.set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(polls) == 2
        hub.unsubscribe(queue)
        ticks.set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(polls) == 2
        task.cancel()

    asyncio.run(scenario())


# ---- through the app ---------------------------------------------------------------------------


def make_app(tmp_path: Path, fetcher: FakeStatuses, clock: Clock):
    claims = tmp_path / "claims"
    app = create_app(
        fetch=fetcher,
        tracker=lambda: None,
        wall_clock=clock,
        claims_dir=lambda: claims,
        claim_tasks=lambda: tracked("devon/a", "devon/b", "devon/c"),
    )
    return TestClient(app), claims


def stacked_repo(tmp_path: Path) -> int:
    return seed_repo(
        tmp_path / "repos",
        "api",
        {"devon/a": "main", "devon/b": "devon/a", "devon/c": "main"},
    )


def test_a_claimed_branch_reads_with_an_agent_and_keeps_what_it_would_be(tmp_path):
    fetcher = FakeStatuses()
    fetcher.statuses["devon/a"] = open_status(open_comments=2)
    clock = Clock()
    client, claims = make_app(tmp_path, fetcher, clock)
    stacked_repo(tmp_path)
    refresh(client)
    write_holder(claims, "s1", claims=[ref_claim("devon/a")], link="claude://x/1")
    client.app.state.claims_feed.poll()  # type: ignore[attr-defined]

    a = branches(client)["devon/a"]

    assert a["status"]["next"] == {
        "label": "With an agent",
        "who": "agent",
        "kind": "claimed",
        "would_be": {
            "label": "Resolve comments",
            "who": "you",
            "kind": "resolve_comments",
        },
    }
    (holder,) = a["claim"]["holders"]
    assert holder["state"] == "working" and holder["stale"] is False
    assert holder["link"] == {"label": "Open session", "href": "claude://x/1"}
    chip = [c for c in a["status"]["now"] if c["dimension"] == "next"][0]
    assert (chip["value"], chip["who"], chip["would_be"]) == (
        "claimed",
        "agent",
        "Resolve comments",
    )
    assert branches(client)["devon/c"]["claim"] == {"holders": []}


def test_an_idle_holder_adds_a_chip_but_leaves_the_status_alone(tmp_path):
    fetcher = FakeStatuses()
    clock = Clock()
    client, claims = make_app(tmp_path, fetcher, clock)
    stacked_repo(tmp_path)
    refresh(client)
    write_holder(claims, "s1", state="blocked", ttl=1800, claims=[ref_claim("devon/a")])
    client.app.state.claims_feed.poll()  # type: ignore[attr-defined]

    a = branches(client)["devon/a"]

    assert a["status"]["next"]["kind"] == "open_pr"
    assert [h["state"] for h in a["claim"]["holders"]] == ["blocked"]


def test_a_merged_pr_stays_done_but_still_shows_the_claim(tmp_path):
    from toolbelt.git.stack.status import BranchStatus, CiState, PrState

    fetcher = FakeStatuses()
    fetcher.statuses["devon/a"] = BranchStatus(
        pr=PrState.MERGED,
        ci=CiState.SUCCESS,
        review=ReviewState.APPROVED,
        url="https://github.com/o/r/pull/9",
    )
    clock = Clock()
    client, claims = make_app(tmp_path, fetcher, clock)
    stacked_repo(tmp_path)
    refresh(client)
    write_holder(claims, "s1", claims=[ref_claim("devon/a")])
    client.app.state.claims_feed.poll()  # type: ignore[attr-defined]

    a = branches(client)["devon/a"]

    assert a["status"]["next"]["kind"] == "done"
    assert len(a["claim"]["holders"]) == 1


def test_a_child_of_a_claimed_branch_waits_on_others(tmp_path):
    fetcher = FakeStatuses()
    fetcher.statuses["devon/a"] = open_status(review=ReviewState.REQUESTED)
    fetcher.statuses["devon/b"] = open_status(review=ReviewState.APPROVED)
    clock = Clock()
    client, claims = make_app(tmp_path, fetcher, clock)
    stacked_repo(tmp_path)
    refresh(client)
    # a's parent state alone: b is blocked on a, who it waits on is "others".
    assert branches(client)["devon/b"]["status"]["next"]["who"] == "others"
    write_holder(claims, "s1", claims=[ref_claim("devon/a")])
    client.app.state.claims_feed.poll()  # type: ignore[attr-defined]

    b = branches(client)["devon/b"]["status"]["next"]

    assert (b["label"], b["who"], b["kind"]) == (
        "Blocked on devon/a",
        "others",
        "blocked",
    )


def test_a_claim_change_is_pushed_for_the_branch_and_its_stacked_children(tmp_path):
    fetcher = FakeStatuses()
    fetcher.statuses["devon/a"] = open_status(open_comments=1)
    fetcher.statuses["devon/b"] = open_status(review=ReviewState.APPROVED)
    clock = Clock()
    client, claims = make_app(tmp_path, fetcher, clock)
    repo_id = stacked_repo(tmp_path)
    refresh(client)
    app = client.app
    queue = app.state.hub.subscribe()  # type: ignore[attr-defined]

    def events() -> list[tuple[str, dict]]:
        out = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out

    write_holder(claims, "s1", claims=[ref_claim("devon/a")])
    app.state.claims_feed.poll()  # type: ignore[attr-defined]
    ((name, data),) = events()

    assert name == "claims"
    TypeAdapter(api_models.ServerEvent).validate_python(
        {"type": name, **data}
    )  # a typed event
    by_id = {p["branch_id"]: p for p in data["branches"]}
    assert set(by_id) == {f"{repo_id}:devon/a", f"{repo_id}:devon/b"}
    assert by_id[f"{repo_id}:devon/a"]["status"]["next"]["kind"] == "claimed"
    assert len(by_id[f"{repo_id}:devon/a"]["claim"]["holders"]) == 1
    assert by_id[f"{repo_id}:devon/b"]["claim"] == {"holders": []}
    b_next = by_id[f"{repo_id}:devon/b"]["status"]["next"]
    assert b_next["kind"] == "blocked" and b_next["who"] == "others"

    # Expiry alone (no file change) is noticed and pushed.
    clock.now += timedelta(seconds=400)
    app.state.claims_feed.poll()  # type: ignore[attr-defined]
    ((name, data),) = events()
    by_id = {p["branch_id"]: p for p in data["branches"]}
    assert by_id[f"{repo_id}:devon/a"]["status"]["next"]["kind"] == "resolve_comments"
    assert by_id[f"{repo_id}:devon/a"]["claim"] == {"holders": []}
    assert (
        by_id[f"{repo_id}:devon/b"]["status"]["next"]["who"] == "you"
    )  # a is mine again


def test_the_snapshot_without_a_claims_directory_has_empty_claims(tmp_path):
    client, _ = make_app(tmp_path, FakeStatuses(), Clock())
    stacked_repo(tmp_path)

    assert all(b["claim"] == {"holders": []} for b in snap(client)["branches"])
    next_filter = [f for f in snap(client)["filters"] if f["key"] == "next"][0]
    assert ("agent", "With an agent") in [
        (o["value"], o["label"]) for o in next_filter["options"]
    ]


def test_the_feed_deletes_expired_files_after_a_grace_and_their_revocations(tmp_path):
    f = Feed(tmp_path)
    write_holder(f.dir, "s1", claims=[ref_claim("devon/a")], updated_ago=5)
    (f.dir / "revoked").mkdir()
    (f.dir / "revoked" / "s1.json").write_text(
        '{"version": 1, "holder": "s1", "revoked": []}'
    )
    f.feed.poll()

    f.clock.now += timedelta(seconds=180)  # expired 5 s ago
    f.feed.poll()
    assert (f.dir / "s1.json").exists()

    f.clock.now += timedelta(seconds=10)
    f.feed.poll()
    assert not (f.dir / "s1.json").exists()
    assert not (f.dir / "revoked" / "s1.json").exists()


def test_a_file_is_reported_unreadable_only_when_bad_on_two_polls_in_a_row(tmp_path):
    f = Feed(tmp_path)
    f.dir.mkdir()
    (f.dir / "bad.json").write_text("{")

    f.feed.poll()
    assert f.feed.unreadable == frozenset()
    f.feed.poll()
    assert f.feed.unreadable == {"bad"}

    write_holder(f.dir, "bad", claims=[])
    f.feed.poll()
    assert f.feed.unreadable == frozenset()


def test_the_snapshot_reads_the_claims_even_before_any_stream_is_open(tmp_path):
    fetcher = FakeStatuses()
    client, claims = make_app(tmp_path, fetcher, Clock())
    stacked_repo(tmp_path)
    refresh(client)
    write_holder(claims, "s1", claims=[ref_claim("devon/c")])

    c = branches(client)["devon/c"]

    assert c["status"]["next"]["kind"] == "claimed"
    assert [h["holder"] for h in c["claim"]["holders"]] == ["s1"]
