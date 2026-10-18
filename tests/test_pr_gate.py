"""The ETag gate: what it learns and when it says nothing changed, how `gh api
--include` output is read, and how the status refresher uses the gate (reuse a
lookup, a full lookup after an interval, webhook hints, repos with webhooks
polled less often)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from toolbelt import logged_process
from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.ui import pr_gate
from toolbelt.ui.live_status import (
    EventHub,
    Fetched,
    StatusRefresher,
    TrackedBranch,
)
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.pr_fetch import PrSnapshot
from toolbelt.ui.pr_gate import ConditionalAnswer, GithubChangeGate, PrGate

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


class TestGhConditionalGet:
    def answer(self, monkeypatch, output: str, code: int = 0, expect_args=None):
        class Process:
            returncode = code

            async def communicate(self):
                return output.encode(), b"boom"

        async def create(*args, **kwargs):
            if expect_args is not None:
                expect_args.extend(args)
            return Process()

        monkeypatch.setattr(logged_process, "create_subprocess_exec", create)

    def test_a_200_carries_the_new_etag(self, monkeypatch):
        self.answer(
            monkeypatch,
            'HTTP/2.0 200 OK\r\nEtag: "abc"\r\nContent-Type: x\r\n\r\n{"a": 1}',
        )

        got = run(pr_gate.gh_conditional_get(PULL, None))

        assert got == ConditionalAnswer(changed=True, etag='"abc"')

    def test_a_304_is_unchanged_and_sends_the_etag_it_was_given(self, monkeypatch):
        args: list[str] = []
        # `gh api` exits 1 for a 304.
        self.answer(
            monkeypatch,
            'HTTP/2.0 304 Not Modified\nEtag: "abc"\nDate: x\n\n',
            code=1,
            expect_args=args,
        )

        got = run(pr_gate.gh_conditional_get(PULL, '"abc"'))

        assert got == ConditionalAnswer(changed=False, etag='"abc"')
        assert args[-2:] == ["-H", 'If-None-Match: "abc"']

    def test_a_failing_call_raises(self, monkeypatch):
        self.answer(monkeypatch, "HTTP/2.0 404 Not Found\n\n", code=1)

        with pytest.raises(RuntimeError, match="boom"):
            run(pr_gate.gh_conditional_get(PULL, None))

    def test_a_call_that_printed_nothing_raises(self, monkeypatch):
        self.answer(monkeypatch, "", code=1)

        with pytest.raises(RuntimeError, match="boom"):
            run(pr_gate.gh_conditional_get(PULL, None))


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
        s.pass_after(20)

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
