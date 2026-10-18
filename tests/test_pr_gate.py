"""The ETag gate: what it learns and when it says nothing changed, how `gh api
--include` output is read, and how the status refresher uses the gate (reuse a
lookup, a full lookup after an interval, webhook hints, repos with webhooks
polled less often)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.ui.github_requests import ConditionalAnswer, RateLimited
from toolbelt.ui.live_status import (
    EventHub,
    Fetched,
    StatusRefresher,
    TrackedBranch,
)
from toolbelt.ui.lookup_cache import SavedLookup
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.pr_fetch import PrSnapshot
from toolbelt.ui.pr_gate import GithubChangeGate, PrGate

OPEN = BranchStatus(
    pr=PrState.OPEN,
    ci=CiState.SUCCESS,
    review=ReviewState.NONE,
    url="https://github.com/o/r/pull/5",
)
FACTS = PrFacts(
    number=5,
    title="Add the thing",
    author="devon",
    base_ref="main",
    head_oid="abc",
    state="OPEN",
    is_draft=False,
    created_at=datetime(2026, 10, 1, tzinfo=UTC),
    merged_at=None,
    closed_at=None,
    requested_reviewers=(),
    commit_rollups=(),
    items=(),
)
PULL = "repos/o/r/pulls/5"
CHECKS = "repos/o/r/commits/abc/check-runs"
STATUS = "repos/o/r/commits/abc/status"


class FakeGitHub:
    """Serves ETags: a path answers 304 when asked with its current ETag."""

    def __init__(self) -> None:
        self.etags: dict[str, str] = {}
        self.asked: list[tuple[str, str | None]] = []
        self.failing = False

    def change(self, path: str) -> None:
        self.etags[path] = f"{self.etags.get(path, 'v')}+"

    async def __call__(self, path: str, etag: str | None) -> ConditionalAnswer:
        self.asked.append((path, etag))
        if self.failing:
            raise RuntimeError("gh")
        current = self.etags.setdefault(path, "v1")
        return ConditionalAnswer(changed=etag != current, etag=current)


def run(coro):
    return asyncio.run(coro)


class TestPrGate:
    def test_a_resource_not_seen_before_counts_as_changed_then_is_remembered(self):
        github = FakeGitHub()
        gate = PrGate(github)

        assert run(gate.unchanged("o/r", 5, "abc")) is False
        assert run(gate.unchanged("o/r", 5, "abc")) is True

    def test_any_one_resource_changing_is_a_change(self):
        github = FakeGitHub()
        gate = PrGate(github)
        run(gate.unchanged("o/r", 5, "abc"))

        github.change(CHECKS)

        assert run(gate.unchanged("o/r", 5, "abc")) is False
        # The new ETag was kept: the next look compares with now.
        assert run(gate.unchanged("o/r", 5, "abc")) is True

    def test_a_new_head_commit_is_a_change(self):
        gate = PrGate(FakeGitHub())
        run(gate.unchanged("o/r", 5, "abc"))

        assert run(gate.unchanged("o/r", 5, "def")) is False

    def test_remember_asks_only_for_what_it_has_not_seen(self):
        github = FakeGitHub()
        gate = PrGate(github)
        run(gate.remember("o/r", 5, "abc"))
        assert sorted(path for path, _ in github.asked) == sorted(
            [PULL, CHECKS, STATUS]
        )

        github.asked.clear()
        run(gate.remember("o/r", 5, "abc"))

        assert github.asked == []
        assert run(gate.unchanged("o/r", 5, "abc")) is True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Setup:
    """A refresher over one branch with a gate backed by ``FakeGitHub``."""

    def __init__(self, *, pushed=False, **kwargs) -> None:
        self.clock = Clock()
        self.github = FakeGitHub()
        self.lookups = 0
        self.changes: list[Fetched] = []
        self.status = OPEN
        self.pushed = pushed
        self.refresher = StatusRefresher(
            branches=lambda: [TrackedBranch(1, "devon/a", Path("/repo"))],
            fetch=self.fetch,
            hub=EventHub(clock=self.clock),
            on_change=lambda branch, fetched: self.changes.append(fetched),
            clock=self.clock,
            interval=20,
            settled_interval=300,
            gate=GithubChangeGate(PrGate(self.github), self.slug_of),
            full_interval=120,
            pushed=lambda repo_id: self.pushed,
            pushed_interval=300,
            **kwargs,
        )

    async def slug_of(self, repo_id: int) -> str | None:
        return "o/r"

    async def fetch(self, branch: str, root: Path) -> PrSnapshot:
        self.lookups += 1
        return PrSnapshot(self.status, FACTS)

    def pass_after(self, seconds: float) -> int:
        self.clock.now += seconds
        return run(self.refresher.run_once())


class TestRefresherWithGate:
    def test_a_poll_that_finds_nothing_new_skips_the_lookup(self):
        s = Setup()
        s.pass_after(0)
        assert s.lookups == 1

        s.pass_after(20)

        assert s.lookups == 1
        cached = s.refresher.get(1, "devon/a")
        assert cached is not None
        assert cached.at == 1020  # verified now, so its age restarts
        assert cached.full_at == 1000
        assert len(s.changes) == 1  # nothing to report

    def test_a_change_runs_the_full_lookup_and_reports_it(self):
        s = Setup()
        s.pass_after(0)
        s.github.change(PULL)
        s.status = replace(OPEN, ci=CiState.FAILED)

        s.pass_after(20)

        assert s.lookups == 2
        assert [c.status.ci for c in s.changes if c.status] == [
            CiState.SUCCESS,
            CiState.FAILED,
        ]

    def test_the_full_lookup_runs_once_the_full_interval_has_passed(self):
        s = Setup()
        s.pass_after(0)
        s.pass_after(100)
        assert s.lookups == 1

        s.pass_after(20)  # 120 s after the last full lookup

        assert s.lookups == 2

    def test_a_gate_that_cannot_answer_falls_back_to_the_full_lookup(self):
        s = Setup()
        s.pass_after(0)
        s.github.failing = True

        s.pass_after(20)

        assert s.lookups == 2

    def test_a_branch_without_a_pr_is_always_looked_up(self):
        s = Setup()

        async def no_pr(branch: str, root: Path) -> PrSnapshot:
            s.lookups += 1
            return PrSnapshot(
                BranchStatus(PrState.NONE, CiState.NONE, ReviewState.NONE, None), None
            )

        s.refresher._fetch = no_pr
        s.pass_after(0)
        s.pass_after(60)  # a branch without a PR is looked up every 60 s

        assert s.lookups == 2
        assert s.github.asked == []

    def test_a_webhook_hint_looks_the_branch_up_at_once_without_the_gate(self):
        s = Setup()
        s.pass_after(0)
        s.github.asked.clear()

        s.refresher.hint({(1, "devon/a")})
        fetched = s.pass_after(1)  # not due for 19 more seconds

        assert fetched == 1
        assert s.lookups == 2
        assert s.github.asked == []  # the gate was not asked

    def test_a_hint_for_one_branch_is_used_up(self):
        s = Setup()
        s.pass_after(0)
        s.refresher.hint({(1, "devon/a")})
        s.pass_after(1)

        assert s.pass_after(1) == 0

    def test_a_repo_with_live_webhooks_is_polled_only_as_a_safety_net(self):
        s = Setup(pushed=True)
        s.pass_after(0)

        assert s.pass_after(100) == 0
        assert s.pass_after(200) == 1  # 300 s after the lookup

    def test_a_hint_wakes_the_pause_between_passes(self):
        s = Setup()
        slept = asyncio.Event()

        async def sleep(seconds: float) -> None:
            await slept.wait()

        refresher = s.refresher
        refresher._sleep = sleep

        async def scenario() -> None:
            pause = asyncio.create_task(refresher._pause())
            await asyncio.sleep(0)
            assert not pause.done()
            refresher.hint({(1, "devon/a")})
            await asyncio.wait_for(pause, 1)

        run(scenario())


class TestEtagsAcrossRestarts:
    def test_exported_etags_let_a_new_gate_say_unchanged_straight_away(self):
        github = FakeGitHub()
        first = PrGate(github)
        run(first.unchanged("o/r", 5, "abc"))
        saved = first.export("o/r", 5, "abc")
        assert sorted(saved) == sorted([PULL, CHECKS, STATUS])

        restarted = PrGate(github)
        restarted.restore(saved)

        assert run(restarted.unchanged("o/r", 5, "abc")) is True

    def test_exporting_what_was_never_seen_gives_nothing(self):
        assert PrGate(FakeGitHub()).export("o/r", 5, "abc") == {}


class FakePersistence:
    """Keeps lookups in a dict, as the stack DB does."""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], SavedLookup] = {}

    def load(self):
        return dict(self.rows)

    def save(self, repo_id, branch, saved):
        self.rows[(repo_id, branch)] = saved

    def delete(self, repo_id, branch):
        self.rows.pop((repo_id, branch), None)


class WallClock:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.start = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=self.clock.now - 1000)


class Flaky(Setup):
    """A setup whose lookups can fail, with saved lookups and a wall clock."""

    def __init__(self, **kwargs) -> None:
        self.failure: Exception | None = None
        self.persistence = FakePersistence()
        self.wall = WallClock(Clock())
        super().__init__(persist=self.persistence, jitter=lambda: 0.5, **kwargs)
        self.clock = self.wall.clock
        self.refresher._clock = self.clock
        self.refresher._wall_clock = self.wall

    async def fetch(self, branch: str, root: Path) -> PrSnapshot:
        if self.failure is not None:
            raise self.failure
        return await super().fetch(branch, root)


class TestLastGoodLookup:
    def test_a_failed_lookup_keeps_the_last_good_one_with_its_age_and_the_error(self):
        s = Flaky()
        s.pass_after(0)
        s.failure = RateLimited("graphql", 60)
        s.github.change(PULL)  # so the gate cannot vouch for the old lookup

        s.pass_after(20)

        cached = s.refresher.get(1, "devon/a")
        assert cached is not None
        assert cached.status == OPEN and cached.facts == FACTS
        assert cached.error == "GitHub graphql rate limit exceeded"
        assert cached.wall_at == s.wall.start  # the age of the good lookup
        assert len(s.changes) == 1  # the card still shows the same status

    def test_the_error_is_cleared_by_the_next_good_lookup(self):
        s = Flaky()
        s.pass_after(0)
        s.failure = RuntimeError("boom")
        s.github.change(PULL)
        s.pass_after(20)
        s.failure = None

        s.pass_after(20)

        cached = s.refresher.get(1, "devon/a")
        assert cached is not None and cached.error is None
        assert cached.wall_at == s.wall.start + timedelta(seconds=40)

    def test_a_branch_never_looked_up_has_no_status_but_has_the_error(self):
        s = Flaky()
        s.failure = RuntimeError("boom")

        s.pass_after(0)

        cached = s.refresher.get(1, "devon/a")
        assert cached is not None
        assert cached.status is None and cached.wall_at is None
        assert cached.error == "RuntimeError: boom"

    def test_the_refreshed_event_carries_each_error(self):
        s = Flaky()
        events: list[dict] = []
        s.refresher._hub.publish = lambda event, data: events.append(data)  # type: ignore[method-assign]
        s.pass_after(0)
        s.failure = RuntimeError("boom")
        s.github.change(PULL)

        s.pass_after(20)

        assert events[-1]["fetched"] == [
            {
                "branch_id": "1:devon/a",
                "fetched_at": "2026-10-18T12:00:00Z",
                "error": "RuntimeError: boom",
            }
        ]


class TestSavedLookups:
    def test_a_full_lookup_is_saved_with_its_etags(self):
        s = Flaky()

        s.pass_after(0)

        saved = s.persistence.rows[(1, "devon/a")]
        assert saved.status == OPEN and saved.facts == FACTS
        assert sorted(saved.etags) == sorted([PULL, CHECKS, STATUS])
        assert saved.fetched_at == saved.full_at == s.wall.start

    def test_a_poll_that_reused_the_lookup_does_not_rewrite_it(self):
        s = Flaky()
        s.pass_after(0)
        saved = s.persistence.rows[(1, "devon/a")]

        s.pass_after(20)

        assert s.persistence.rows[(1, "devon/a")] is saved

    def test_a_restarted_server_shows_the_saved_lookup_and_verifies_it_for_free(self):
        old = Flaky()
        old.pass_after(0)
        fresh = Flaky()
        fresh.github = old.github
        fresh.refresher._gate = GithubChangeGate(PrGate(old.github), fresh.slug_of)
        fresh.persistence.rows = dict(old.persistence.rows)
        fresh.clock.now = 1000 + 100  # the server was down for 100 s
        run(fresh.refresher.restore())

        cached = fresh.refresher.get(1, "devon/a")
        assert cached is not None
        assert cached.status == OPEN
        assert cached.wall_at == old.wall.start  # as old as it really is

        fresh.lookups = 0
        run(fresh.refresher.run_once())

        assert fresh.lookups == 0  # the ETags said nothing changed

    def test_overdue_full_lookups_after_a_restart_are_spread_over_the_interval(self):
        old = Flaky()
        old.pass_after(0)
        fresh = Flaky()
        fresh.persistence.rows = dict(old.persistence.rows)
        fresh.clock.now = 1000 + 100_000  # down for a day

        run(fresh.refresher.restore())

        cached = fresh.refresher.get(1, "devon/a")
        assert cached is not None
        # jitter 0.5: half the 120 s interval ago, so due in 60 s, not now.
        assert fresh.clock.now - cached.full_at == 60

    def test_a_branch_no_longer_tracked_is_dropped_from_the_saved_lookups(self):
        s = Flaky()
        s.pass_after(0)
        s.refresher._branches = list

        run(s.refresher.run_once())

        assert s.persistence.rows == {}


class TestSharedSnapshot:
    """The autonomous agents read PRs through the refresher."""

    def branch(self) -> TrackedBranch:
        return TrackedBranch(1, "devon/a", Path("/repo"))

    def test_a_lookup_younger_than_max_age_is_reused(self):
        s = Flaky()
        s.pass_after(0)
        s.lookups = 0
        asked = len(s.github.asked)

        snapshot = run(s.refresher.snapshot(self.branch(), max_age=300))

        assert snapshot == PrSnapshot(OPEN, FACTS)
        assert s.lookups == 0
        assert len(s.github.asked) == asked  # no gate traffic either

    def test_an_older_one_is_checked_through_the_gate_first(self):
        s = Flaky()
        s.pass_after(0)
        s.clock.now += 100  # older than max_age, within the full interval
        s.lookups = 0
        asked = len(s.github.asked)

        snapshot = run(s.refresher.snapshot(self.branch(), max_age=60))

        assert snapshot.status == OPEN
        assert s.lookups == 0  # the gate said nothing changed
        assert len(s.github.asked) == asked + 3

    def test_a_branch_never_looked_up_is_looked_up_and_kept(self):
        s = Flaky()

        run(s.refresher.snapshot(self.branch(), max_age=300))

        assert s.lookups == 1
        assert s.refresher.get(1, "devon/a") is not None
        assert (1, "devon/a") in s.persistence.rows

    def test_it_raises_when_the_latest_lookup_failed(self):
        s = Flaky()
        s.pass_after(0)
        s.failure = RateLimited("graphql", 60)
        s.github.change(PULL)
        s.clock.now += 400

        with pytest.raises(RuntimeError, match="rate limit"):
            run(s.refresher.snapshot(self.branch(), max_age=300))
