"""PRs queued to merge when CI passes: what may be queued, when the queue merges
and when it must not. Lookups and the merge itself are faked."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import MergeQueueRow
from toolbelt.ui.live_status import Fetched, TrackedBranch
from toolbelt.ui.merge_queue import LandResult, MergeQueue, MergeQueueRefused
from toolbelt.ui.pr_activity import PrFacts

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def looked_up(*, head="h1", pr=PrState.OPEN, number=7) -> Fetched:
    status = BranchStatus(
        pr=pr,
        ci=CiState.RUNNING,
        review=ReviewState.APPROVED,
        url=f"https://github.com/o/r/pull/{number}",
    )
    facts = PrFacts(
        number=number,
        title="t",
        author="devon",
        base_ref="main",
        head_oid=head,
        state="OPEN",
        is_draft=False,
        created_at=NOW - timedelta(days=1),
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=(),
    )
    return Fetched(status=status, at=0.0, facts=facts)


class Rig:
    def __init__(self) -> None:
        self.rows: list[MergeQueueRow] = []
        self.tracked = ["a", "b"]
        self.fetched: dict[str, Fetched | None] = {
            "a": looked_up(number=1),
            "b": looked_up(number=2),
        }
        self.kinds: dict[str, str | None] = {"a": "wait_ci", "b": "wait_ci"}
        self.would = {"a": True, "b": True}
        self.landed: list[str] = []
        self.changed: list[set] = []
        self.result = LandResult(ok=True, output="merged")
        self.gate: asyncio.Event | None = None
        self.running = 0
        self.most_running = 0
        self.slept = 0
        self.queue = self.make_queue()

    def make_queue(self) -> MergeQueue:
        return MergeQueue(
            entries=lambda: list(self.rows),
            add=lambda row: self.rows.append(row),
            remove=lambda repo_id, branch: self.rows.__setitem__(
                slice(None),
                [r for r in self.rows if (r.repo_id, r.branch) != (repo_id, branch)],
            ),
            branches=lambda: [
                TrackedBranch(1, name, Path("/r")) for name in self.tracked
            ],
            fetched=lambda repo_id, name: self.fetched.get(name),
            next_kind=lambda repo_id, name: self.kinds.get(name),
            would_merge=lambda repo_id, name: self.would.get(name, False),
            land=self.land,
            wall_clock=lambda: NOW,
            changed=self.changed.append,
            sleep=self.sleep,
            tick=10,
        )

    async def land(self, repo_id: int, name: str) -> LandResult:
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        if self.gate is not None:
            await self.gate.wait()
        self.landed.append(name)
        self.running -= 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def sleep(self, seconds: float) -> None:
        self.slept += 1
        if self.slept >= 3:
            raise asyncio.CancelledError

    def merge_now(self, *names: str) -> None:
        """Their CI passed: the next thing is to merge."""
        for name in names:
            self.kinds[name] = "merge"

    def run(self) -> None:
        """One check, and the merges it started, to the end."""

        async def go() -> None:
            self.queue.check()
            await self.queue.idle()

        asyncio.run(go())

    def info(self, name: str = "a"):
        return self.queue.info(1, name)


# ---- queueing ------------------------------------------------------------------


def test_a_pr_that_is_only_waiting_on_ci_can_be_queued():
    rig = Rig()

    info = rig.info()

    assert info is not None and info.can_queue and not info.queued


def test_a_pr_that_is_not_waiting_only_on_ci_has_nothing_to_say():
    rig = Rig()
    rig.would["a"] = False

    assert rig.info() is None


def test_queueing_records_the_head_it_was_approved_at_and_says_it_is_queued():
    rig = Rig()
    rig.fetched["a"] = looked_up(head="abc123", number=1)

    info = rig.queue.queue(1, "a")

    assert info.queued and not info.can_queue and info.queued_at == NOW.isoformat()
    assert [(r.branch, r.pr_number, r.head_oid, r.queued_at) for r in rig.rows] == [
        ("a", 1, "abc123", NOW)
    ]
    assert rig.changed == [{(1, "a")}]


def test_a_pr_that_is_not_ready_to_wait_for_ci_is_refused():
    rig = Rig()
    rig.would["a"] = False

    with pytest.raises(MergeQueueRefused, match="approved"):
        rig.queue.queue(1, "a")

    assert rig.rows == []


def test_a_pr_that_has_not_been_looked_up_is_refused():
    rig = Rig()
    rig.fetched["a"] = None

    with pytest.raises(MergeQueueRefused, match="looked up"):
        rig.queue.queue(1, "a")


def test_queueing_twice_keeps_one_entry():
    rig = Rig()

    rig.queue.queue(1, "a")
    rig.queue.queue(1, "a")

    assert len(rig.rows) == 1


def test_the_queue_survives_a_restart():
    rig = Rig()
    rig.queue.queue(1, "a")

    restarted = rig.make_queue()

    info = restarted.info(1, "a")
    assert info is not None and info.queued


def test_cancelling_takes_it_off_the_queue_without_a_note():
    rig = Rig()
    rig.queue.queue(1, "a")

    rig.queue.cancel(1, "a")

    assert rig.rows == [] and rig.info() is not None and rig.info().notice is None  # type: ignore[union-attr]
    assert rig.info().queued is False  # type: ignore[union-attr]


# ---- merging once CI has passed ---------------------------------------------------


def test_a_queued_pr_still_waiting_on_ci_is_left_alone():
    rig = Rig()
    rig.queue.queue(1, "a")

    rig.run()

    assert rig.landed == [] and len(rig.rows) == 1


def test_a_queued_pr_whose_ci_passed_is_merged_and_leaves_the_queue():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.merge_now("a")
    rig.changed.clear()

    rig.run()

    assert rig.landed == ["a"]
    assert rig.rows == []
    assert rig.changed == [{(1, "a")}, {(1, "a")}]  # merging began, then it was done
    info = rig.info()
    assert info is not None and info.notice is None and not info.queued


def test_a_pr_that_is_not_queued_is_never_merged_by_the_queue():
    rig = Rig()
    rig.merge_now("a")

    rig.run()

    assert rig.landed == []


def test_while_it_merges_the_card_says_so():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.merge_now("a")
    rig.gate = asyncio.Event()
    seen = []

    async def go() -> None:
        rig.queue.check()
        await asyncio.sleep(0)
        seen.append(rig.info())
        rig.gate.set()  # type: ignore[union-attr]
        await rig.queue.idle()

    asyncio.run(go())

    assert seen[0] is not None and seen[0].merging and seen[0].queued


def test_a_merge_that_fails_leaves_the_queue_and_says_why():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.merge_now("a")
    rig.result = LandResult(ok=False, output="x" * 1000 + "GraphQL: not mergeable")

    rig.run()

    info = rig.info()
    assert rig.rows == []
    assert info is not None and info.notice is not None
    assert info.notice.startswith("The queued merge failed: ")
    assert info.notice.endswith("GraphQL: not mergeable") and len(info.notice) < 700


def test_a_merge_that_raises_leaves_the_queue_and_says_why():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.merge_now("a")
    rig.result = RuntimeError("boom")  # type: ignore[assignment]

    rig.run()

    info = rig.info()
    assert rig.rows == [] and info is not None and "boom" in (info.notice or "")


def test_queued_prs_merge_one_at_a_time():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.queue.queue(1, "b")
    rig.merge_now("a", "b")
    rig.gate = asyncio.Event()

    async def go() -> None:
        rig.queue.check()
        await asyncio.sleep(0)
        rig.gate.set()  # type: ignore[union-attr]
        await rig.queue.idle()

    asyncio.run(go())

    assert sorted(rig.landed) == ["a", "b"] and rig.most_running == 1


# ---- what must not be merged ------------------------------------------------------


def test_new_commits_after_queueing_mean_it_is_not_merged():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.fetched["a"] = looked_up(head="pushed-later", number=1)
    rig.merge_now("a")

    rig.run()

    info = rig.info()
    assert rig.landed == [] and rig.rows == []
    assert info is not None and "New commits" in (info.notice or "")


@pytest.mark.parametrize(
    ("kind", "why"),
    [
        ("fix_ci", "CI failed"),
        ("resolve_comments", "unresolved comments"),
        ("resolve_conflicts", "conflicts"),
        ("address_review", "changes were requested"),
        ("blocked", "blocked on a PR"),
        ("wait_review", "approval was removed"),
        ("publish", "draft"),
        ("something_new", "something_new"),
    ],
)
def test_a_pr_that_stops_being_one_ci_wait_from_merging_is_taken_off_with_the_reason(
    kind: str, why: str
):
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.kinds["a"] = kind

    rig.run()

    info = rig.info()
    assert rig.landed == [] and rig.rows == []
    assert info is not None and why in (info.notice or "")


@pytest.mark.parametrize("held", ["claimed", "autonomous"])
def test_a_pr_an_agent_holds_stays_queued_until_it_is_free(held: str):
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.kinds["a"] = held

    rig.run()

    assert rig.landed == [] and len(rig.rows) == 1


def test_a_pr_merged_some_other_way_leaves_the_queue_quietly():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.fetched["a"] = looked_up(pr=PrState.MERGED, number=1)
    rig.would["a"] = False  # a merged PR is not waiting on anything

    rig.run()

    assert rig.landed == [] and rig.rows == []
    assert rig.info() is None  # and there is no note


def test_a_pr_closed_without_merging_leaves_the_queue_with_a_note():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.fetched["a"] = looked_up(pr=PrState.CLOSED, number=1)

    rig.run()

    info = rig.info()
    assert rig.rows == [] and info is not None and "closed" in (info.notice or "")


def test_a_branch_that_is_no_longer_tracked_leaves_the_queue_quietly():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.tracked = ["b"]

    rig.run()

    assert rig.rows == [] and rig.landed == []


def test_a_pr_not_looked_up_yet_is_left_queued():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.fetched["a"] = None
    rig.merge_now("a")

    rig.run()

    assert rig.landed == [] and len(rig.rows) == 1


def test_queueing_again_clears_the_note_of_the_last_time():
    rig = Rig()
    rig.queue.queue(1, "a")
    rig.kinds["a"] = "fix_ci"
    rig.run()
    assert rig.info() is not None and rig.info().notice  # type: ignore[union-attr]
    rig.kinds["a"] = "wait_ci"

    info = rig.queue.queue(1, "a")

    assert info.notice is None and info.queued


def test_the_watcher_checks_the_queue_every_tick():
    rig = Rig()
    rig.queue.queue(1, "a")

    async def go() -> None:
        try:
            await rig.queue.run()
        except asyncio.CancelledError:
            pass

    rig.merge_now("a")
    asyncio.run(go())

    assert rig.landed == ["a"]
