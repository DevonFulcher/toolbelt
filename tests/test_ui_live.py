"""The live half of `tt ui`: the status cache/poller, the event hub, the
stack-DB watcher and the `/events` stream.

Time is a fake clock and sleeping is a gate the test opens, so a polling pass
runs exactly when the test says; nothing waits on real time or on `gh`. The
event stream is driven through the raw ASGI interface because Starlette's
TestClient buffers a whole response and an event stream never ends.
"""

import asyncio
import json
import re
from pathlib import Path

from toolbelt.git.stack.status import (
    NO_PR,
    BranchStatus,
    CiState,
    PrState,
    ReviewState,
)
from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.ui.app import cell_id, create_app
from toolbelt.ui.pr_fetch import PrSnapshot
from toolbelt.ui.live_status import (
    EventHub,
    Fetched,
    StatusRefresher,
    StructureWatcher,
    TrackedBranch,
)

OPEN = BranchStatus(
    pr=PrState.OPEN,
    ci=CiState.SUCCESS,
    review=ReviewState.NONE,
    url="https://github.com/o/r/pull/5",
)
MERGED = BranchStatus(
    pr=PrState.MERGED,
    ci=CiState.SUCCESS,
    review=ReviewState.APPROVED,
    url="https://github.com/o/r/pull/6",
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Fetcher:
    """A status fetcher serving seeded statuses; tracks peak concurrency."""

    def __init__(self) -> None:
        self.statuses: dict[str, BranchStatus] = {}
        self.calls: list[str] = []
        self.failing: set[str] = set()
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, branch: str, root: Path) -> PrSnapshot:
        self.calls.append(branch)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0)  # let the others start, as real I/O would
            if branch in self.failing:
                raise FileNotFoundError("gh")
            return PrSnapshot(self.statuses.get(branch, NO_PR), None)
        finally:
            self.in_flight -= 1


class Setup:
    def __init__(self, names: list[str], *, grace: float = 0, **kwargs) -> None:
        self.names = names
        self.clock = Clock()
        self.fetcher = Fetcher()
        self.hub = EventHub(clock=self.clock, grace=grace)
        self.changes: list[tuple[str, Fetched]] = []
        self.refresher = StatusRefresher(
            branches=lambda: [
                TrackedBranch(1, name, Path("/repo")) for name in self.names
            ],
            fetch=self.fetcher,
            hub=self.hub,
            on_change=lambda branch, fetched: self.changes.append(
                (branch.branch, fetched)
            ),
            clock=self.clock,
            interval=20,
            settled_interval=300,
            **kwargs,
        )

    def changed_branches(self) -> list[str]:
        return [name for name, _ in self.changes]


def run(coro):
    return asyncio.run(coro)


class TestStatusRefresher:
    def test_first_pass_fetches_everything_and_reports_each_as_a_change(self):
        s = Setup(["a", "b"])
        s.fetcher.statuses["a"] = OPEN

        fetched = run(s.refresher.run_once())

        assert fetched == 2
        assert sorted(s.changed_branches()) == ["a", "b"]
        cached = s.refresher.get(1, "a")
        assert cached is not None and cached.status == OPEN
        assert s.refresher.get(1, "nope") is None

    def test_nothing_is_refetched_before_its_interval(self):
        s = Setup(["a"])

        async def scenario():
            await s.refresher.run_once()
            s.clock.now += 19
            assert await s.refresher.run_once() == 0
            s.clock.now += 1
            assert await s.refresher.run_once() == 1

        run(scenario())
        assert s.fetcher.calls == ["a", "a"]

    def test_only_changes_are_reported(self):
        s = Setup(["a"])

        async def scenario():
            await s.refresher.run_once()
            s.clock.now += 20
            await s.refresher.run_once()  # same status again
            s.fetcher.statuses["a"] = OPEN
            s.clock.now += 20
            await s.refresher.run_once()

        run(scenario())
        assert s.changed_branches() == ["a", "a"]
        assert s.changes[-1][1].status == OPEN

    def test_merged_and_closed_prs_are_polled_slowly(self):
        s = Setup(["merged", "open"])
        s.fetcher.statuses = {"merged": MERGED, "open": OPEN}

        async def scenario():
            await s.refresher.run_once()
            s.clock.now += 20
            assert await s.refresher.run_once() == 1  # only the open one
            s.clock.now += 279
            assert await s.refresher.run_once() == 1  # open again, merged not
            assert s.fetcher.calls.count("merged") == 1
            s.clock.now += 1
            assert await s.refresher.run_once() == 1  # merged is 300s old now
            assert s.fetcher.calls.count("merged") == 2

        run(scenario())

    def test_concurrency_is_bounded(self):
        s = Setup([f"b{i}" for i in range(10)], concurrency=3)

        run(s.refresher.run_once())

        assert len(s.fetcher.calls) == 10
        assert s.fetcher.peak == 3

    def test_a_failed_lookup_is_recorded_and_does_not_stop_the_others(self):
        s = Setup(["bad", "good"])
        s.fetcher.failing = {"bad"}
        s.fetcher.statuses["good"] = OPEN

        run(s.refresher.run_once())

        bad = s.refresher.get(1, "bad")
        good = s.refresher.get(1, "good")
        assert bad is not None and bad.status is None
        assert good is not None and good.status == OPEN
        assert sorted(s.changed_branches()) == ["bad", "good"]

    def test_branches_that_disappear_are_forgotten(self):
        s = Setup(["a", "b"])

        async def scenario():
            await s.refresher.run_once()
            s.names.remove("b")
            await s.refresher.run_once()

        run(scenario())
        assert s.refresher.get(1, "b") is None
        assert s.refresher.get(1, "a") is not None

    def test_a_pass_publishes_refreshed_and_tracks_its_age(self):
        s = Setup(["a"])

        async def scenario():
            queue = s.hub.subscribe()
            assert s.refresher.seconds_since_refresh() is None
            await s.refresher.run_once()
            s.clock.now += 7
            assert s.refresher.seconds_since_refresh() == 7
            return queue.get_nowait()

        event = run(scenario())
        assert event is not None and event[0] == "refreshed"

    def test_polls_only_while_a_browser_is_connected(self):
        async def scenario():
            wake = asyncio.Event()

            async def sleep(_seconds: float) -> None:
                await wake.wait()
                wake.clear()

            s = Setup(["a"], sleep=sleep)
            task = asyncio.create_task(s.refresher.run())

            async def settle() -> None:
                for _ in range(10):
                    await asyncio.sleep(0)

            await settle()
            assert s.fetcher.calls == []  # nobody is watching

            queue = s.hub.subscribe()
            await settle()
            assert s.fetcher.calls == ["a"]

            s.hub.unsubscribe(queue)
            s.clock.now += 100  # due again, but nobody is watching
            wake.set()
            await settle()
            assert s.fetcher.calls == ["a"]

            s.hub.subscribe()
            await settle()
            assert s.fetcher.calls == ["a", "a"]
            task.cancel()

        run(scenario())


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class TestPollingFollowsViewing:
    """The poller runs while a browser's stream is open (the page closes it
    when hidden), for a grace period after, and wakes the moment one opens."""

    def test_a_returning_browser_refetches_what_went_stale_without_waiting_a_tick(
        self,
    ):
        async def scenario():
            ticks = asyncio.Event()

            async def sleep(_seconds: float) -> None:
                await ticks.wait()  # a tick only passes when the test says
                ticks.clear()

            s = Setup(["stale", "fresh"], sleep=sleep)
            task = asyncio.create_task(s.refresher.run())
            queue = s.hub.subscribe()
            await settle()
            assert sorted(s.fetcher.calls) == ["fresh", "stale"]

            s.hub.unsubscribe(queue)  # tab hidden: the stream is closed
            ticks.set()
            await settle()  # the loop notices nobody is watching and parks
            s.clock.now += 25  # both are past the 20s interval now
            s.fetcher.calls.clear()

            s.hub.subscribe()  # tab visible again
            await settle()
            assert sorted(s.fetcher.calls) == ["fresh", "stale"]
            task.cancel()

        run(scenario())

    def test_only_stale_entries_are_refetched_on_return(self):
        async def scenario():
            ticks = asyncio.Event()

            async def sleep(_seconds: float) -> None:
                await ticks.wait()
                ticks.clear()

            s = Setup(["a", "b"], sleep=sleep)
            task = asyncio.create_task(s.refresher.run())
            queue = s.hub.subscribe()
            await settle()
            s.hub.unsubscribe(queue)
            ticks.set()
            await settle()
            s.clock.now += 10  # younger than the 20s interval
            s.fetcher.calls.clear()

            s.hub.subscribe()
            await settle()
            assert s.fetcher.calls == []
            task.cancel()

        run(scenario())

    def test_polling_carries_on_through_the_grace_period_then_stops(self):
        async def scenario():
            ticks = asyncio.Event()

            async def sleep(_seconds: float) -> None:
                await ticks.wait()
                ticks.clear()

            s = Setup(["a"], sleep=sleep, grace=15)
            task = asyncio.create_task(s.refresher.run())
            queue = s.hub.subscribe()
            await settle()
            assert s.fetcher.calls == ["a"]

            s.hub.unsubscribe(queue)
            s.clock.now += 14  # still within the grace period
            assert s.hub.watched()
            s.clock.now += 6  # 20s since the last fetch, 20s > grace
            ticks.set()
            await settle()
            # Past the grace period at that tick: the loop parked, no fetch.
            assert not s.hub.watched()
            assert s.fetcher.calls == ["a"]
            task.cancel()

        run(scenario())

    def test_a_browser_returning_after_the_grace_period_gets_a_fresh_pass(self):
        async def scenario():
            ticks = asyncio.Event()

            async def sleep(_seconds: float) -> None:
                await ticks.wait()
                ticks.clear()

            s = Setup(["a"], sleep=sleep, grace=15)
            task = asyncio.create_task(s.refresher.run())
            queue = s.hub.subscribe()
            await settle()
            s.hub.unsubscribe(queue)
            s.clock.now += 8
            ticks.set()
            await settle()
            assert s.hub.watched()  # a tick ran inside the grace period
            s.clock.now += 20  # due again; the grace period is long over...
            s.hub.subscribe()  # ...but the browser is back first
            await settle()
            assert s.fetcher.calls == ["a", "a"]
            task.cancel()

        run(scenario())

    def test_unsubscribing_the_last_stream_is_noticed_by_the_hub(self):
        async def scenario():
            hub = EventHub(clock=Clock(), grace=0)
            assert not hub.watched()
            queue = hub.subscribe()
            assert hub.watched()
            hub.unsubscribe(queue)
            return hub.watched()

        assert run(scenario()) is False


class TestEventHub:
    def test_events_reach_every_subscriber_with_a_version(self):
        async def scenario():
            hub = EventHub()
            one, two = hub.subscribe(), hub.subscribe()
            hub.publish("status", {"html": "x"})
            hub.publish("structure", {})
            assert hub.subscribers == 2
            return [one.get_nowait(), one.get_nowait(), two.get_nowait()], hub

        events, hub = run(scenario())
        assert events[0] == ("status", {"html": "x", "v": 1})
        assert events[1] == ("structure", {"v": 2})
        assert events[2] == events[0]
        assert hub.version == 2

    def test_unsubscribed_queues_get_nothing(self):
        async def scenario():
            hub = EventHub()
            queue = hub.subscribe()
            hub.unsubscribe(queue)
            hub.publish("status", {})
            return queue.empty(), hub.subscribers

        assert run(scenario()) == (True, 0)


class TestStructureWatcher:
    def test_a_changed_db_version_publishes_structure_once(self):
        version = [1]

        async def scenario():
            hub = EventHub()
            queue = hub.subscribe()
            watcher = StructureWatcher(version=lambda: version[0], hub=hub)
            assert watcher.check() is False  # baseline
            assert watcher.check() is False
            version[0] = 2
            assert watcher.check() is True
            assert watcher.check() is False
            return queue.get_nowait(), queue.empty()

        event, empty = run(scenario())
        assert event is not None and event[0] == "structure"
        assert empty


# ---- the /events endpoint ---------------------------------------------------


class Stream:
    """One `GET /events` against the app, read message by message."""

    def __init__(self, app) -> None:
        self._app = app
        self._sent: asyncio.Queue[dict] = asyncio.Queue()
        self._received: asyncio.Queue[dict] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "Stream":
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/events",
            "raw_path": b"/events",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "scheme": "http",
            "root_path": "",
        }

        async def receive() -> dict:
            return await self._sent.get()

        async def send(message: dict) -> None:
            await self._received.put(message)

        self._task = asyncio.create_task(self._app(scope, receive, send))
        start = await self._received.get()
        assert start["type"] == "http.response.start"
        self.headers = {k.decode(): v.decode() for k, v in start["headers"]}
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._task is not None
        await self._sent.put({"type": "http.disconnect"})
        await asyncio.wait_for(self._task, 5)

    async def next(self) -> tuple[str, dict] | str:
        """The next event as ``(name, data)``, or a comment line's text."""
        message = await self._received.get()
        text = message["body"].decode()
        if text.startswith(":"):
            return text.strip()
        lines = dict(line.split(": ", 1) for line in text.strip().split("\n"))
        return lines["event"], json.loads(lines["data"])


def seeded_app(tmp_path: Path, **kwargs):
    store = StackStore(default_db_path())
    path = tmp_path / "api"
    path.mkdir()
    repo_id = store.ensure_repo(
        RepoIdentity(git_common_dir=path / ".git", path=path, remote_url=None)
    )
    store.set_parent(repo_id, "devon/a", "main")
    store.close()
    fetcher = Fetcher()
    fetcher.statuses["devon/a"] = OPEN
    app = create_app(fetch=fetcher, **kwargs)
    return app, fetcher, repo_id


def test_events_stream_says_hello_then_relays_status_and_refresh(tmp_path):
    app, _, repo_id = seeded_app(tmp_path)

    async def scenario():
        async with Stream(app) as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            name, hello = await stream.next()
            assert (name, hello["v"], hello["age"]) == ("hello", 0, None)
            assert app.state.hub.subscribers == 1

            await app.state.refresher.run_once()

            name, status = await stream.next()
            assert name == "status"
            assert f"c-{repo_id}-" in status["html"]
            assert "#5 open" in status["html"]
            assert "<tr" not in status["html"]
            name, refreshed = await stream.next()
            assert (name, refreshed["v"]) == ("refreshed", 2)
        # The disconnect is noticed: the poller would pause again.
        assert app.state.hub.subscribers == 0

    run(scenario())


def test_a_status_change_also_republishes_the_branches_stacked_on_it(tmp_path):
    store = StackStore(default_db_path())
    path = tmp_path / "api"
    path.mkdir()
    repo_id = store.ensure_repo(
        RepoIdentity(git_common_dir=path / ".git", path=path, remote_url=None)
    )
    store.set_parent(repo_id, "devon/a", "main")
    store.set_parent(repo_id, "devon/b", "devon/a")
    store.close()
    fetcher = Fetcher()
    approved = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.APPROVED,
        url="https://github.com/o/r/pull/2",
    )
    fetcher.statuses = {"devon/a": OPEN, "devon/b": approved}
    app = create_app(fetch=fetcher)

    async def scenario():
        async with Stream(app) as stream:
            await stream.next()  # hello
            await app.state.refresher.run_once()
            html = ""
            for _ in range(5):
                name, data = await stream.next()
                if name == "status":
                    html += data["html"]
                if name == "refreshed":
                    break
            return html

    html = run(scenario())

    assert "Blocked on devon/a" in html


def test_closing_the_stream_stops_the_polling_after_the_grace_period(tmp_path):
    clock = Clock()
    app, _, _ = seeded_app(tmp_path, clock=clock, grace_seconds=15)

    async def scenario():
        hub = app.state.hub
        assert not hub.watched()  # nobody has ever connected
        async with Stream(app) as stream:
            await stream.next()  # hello
            assert hub.watched()
        # The page closed its stream (tab hidden): still polled for a moment...
        assert hub.subscribers == 0 and hub.watched()
        clock.now += 14
        assert hub.watched()
        clock.now += 1
        # ...then the poller parks.
        assert not hub.watched()

    run(scenario())


def test_structure_change_is_pushed(tmp_path):
    version = [1]
    app, _, _ = seeded_app(tmp_path, db_version=lambda: version[0])

    async def scenario():
        async with Stream(app) as stream:
            await stream.next()  # hello
            app.state.structure_watcher.check()  # baseline
            version[0] += 1
            app.state.structure_watcher.check()
            name, _ = await stream.next()
            assert name == "structure"

    run(scenario())


def test_idle_streams_send_keepalives(tmp_path):
    app, _, _ = seeded_app(tmp_path, keepalive_seconds=0)

    async def scenario():
        async with Stream(app) as stream:
            await stream.next()  # hello
            assert await stream.next() == ": keepalive"

    run(scenario())


def test_a_reconnecting_page_can_tell_it_missed_events(tmp_path):
    app, _, _ = seeded_app(tmp_path)

    async def scenario():
        app.state.hub.publish("structure", {})  # while nobody was listening
        async with Stream(app) as stream:
            name, hello = await stream.next()
            return name, hello["v"]

    assert run(scenario()) == ("hello", 1)


def test_hello_reports_how_stale_the_cache_is(tmp_path):
    clock = Clock()
    app, _, _ = seeded_app(tmp_path, clock=clock)

    async def scenario():
        await app.state.refresher.run_once()
        clock.now += 12
        async with Stream(app) as stream:
            _, hello = await stream.next()
            return hello["age"]

    assert run(scenario()) == 12


def test_the_page_carries_the_event_version_it_was_rendered_at(tmp_path):
    from fastapi.testclient import TestClient

    app, _, _ = seeded_app(tmp_path)
    app.state.hub.publish("structure", {})

    html = TestClient(app).get("/").text

    assert 'data-version="1"' in html


def test_lifespan_runs_and_stops_the_background_loops(tmp_path):
    from fastapi.testclient import TestClient

    app, fetcher, _ = seeded_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        # Nobody is subscribed, so nothing is polled.
        assert fetcher.calls == []
    assert app.state.hub.subscribers == 0


def test_an_empty_database_has_nothing_to_poll():
    s = Setup([])
    assert run(s.refresher.run_once()) == 0


def test_a_change_republishes_every_descendant_not_only_children(tmp_path):
    # c is blocked on b, and who that waits on follows b's own next action,
    # which follows a's: a change to a alone must refresh c's cells too.
    store = StackStore(default_db_path())
    path = tmp_path / "api"
    path.mkdir()
    repo_id = store.ensure_repo(
        RepoIdentity(git_common_dir=path / ".git", path=path, remote_url=None)
    )
    store.set_parent(repo_id, "devon/a", "main")
    store.set_parent(repo_id, "devon/b", "devon/a")
    store.set_parent(repo_id, "devon/c", "devon/b")
    store.close()
    approved = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.APPROVED,
        url="https://github.com/o/r/pull/2",
    )
    waiting = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.REQUESTED,
        url="https://github.com/o/r/pull/1",
    )
    fetcher = Fetcher()
    fetcher.statuses = {"devon/a": waiting, "devon/b": approved, "devon/c": approved}
    clock = Clock()
    app = create_app(fetch=fetcher, clock=clock)
    queue = app.state.hub.subscribe()

    def drain() -> list[tuple[str, dict]]:
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return events

    def next_value(events: list, branch: str) -> str | None:
        found = None
        for name, data in events:
            if name != "status":
                continue
            match = re.search(
                rf'id="{cell_id(repo_id, branch, "next")}"[^>]*data-value="(\w*)"',
                data["html"],
            )
            if match:
                found = match.group(1)
        return found

    async def scenario():
        await app.state.refresher.run_once()
        first = drain()
        # a now needs the user: fix CI.
        fetcher.statuses["devon/a"] = BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.FAILED,
            review=ReviewState.NONE,
            url="https://github.com/o/r/pull/1",
        )
        clock.now += 1000
        await app.state.refresher.run_once()
        return first, drain()

    first, second = run(scenario())

    assert next_value(first, "devon/c") == "others"
    assert next_value(second, "devon/c") == "you"
