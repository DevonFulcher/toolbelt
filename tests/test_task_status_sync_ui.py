"""`tt ui` moves a task to in review once its branch's PR is open: when, how
often, and what is never touched. The tracker is a fake."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.tasks.tracker import StatusMove
from toolbelt.ui.live_status import Fetched, TrackedBranch
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.task_status_sync import TaskStatusSync

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def looked_up(pr=PrState.OPEN, number=7) -> Fetched:
    status = BranchStatus(
        pr=pr, ci=CiState.SUCCESS, review=ReviewState.REQUESTED, url="https://x/pull/1"
    )
    facts = PrFacts(
        number=number,
        title="t",
        author="devon",
        base_ref="main",
        head_oid="h",
        state="OPEN",
        is_draft=pr is PrState.DRAFT,
        created_at=NOW - timedelta(days=1),
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=(),
    )
    return Fetched(status=status, at=0.0, facts=facts)


class Tracker:
    name = "jira"

    def __init__(self, outcome="moved") -> None:
        self.outcome = outcome
        self.moves: list[tuple[str, str]] = []

    def move(self, ref, stage):
        self.moves.append((ref, stage))
        return StatusMove(self.outcome, f"{ref}: message")  # type: ignore[arg-type]


class Rig:
    def __init__(self) -> None:
        self.tracker = Tracker()
        self.fetched = {"a": looked_up(), "b": looked_up(PrState.DRAFT, 8)}
        self.tasks: dict[str, str | None] = {"a": "RD-1", "b": "RD-2"}
        self.done: set[tuple[int, str, str, int]] = set()
        self.tracked = ["a", "b"]
        self.sync = TaskStatusSync(
            branches=lambda: [TrackedBranch(1, n, Path("/r")) for n in self.tracked],
            fetched=lambda repo_id, name: self.fetched.get(name),
            linked=lambda repo_id, name: (
                (self.tasks[name], self.tracker) if self.tasks.get(name) else None
            ),
            moved=lambda repo_id, name, stage, number: (
                (repo_id, name, stage, number) in self.done
            ),
            record=lambda repo_id, name, stage, number, at: self.done.add(
                (repo_id, name, stage, number)
            ),
            wall_clock=lambda: NOW,
        )

    def check(self) -> None:
        asyncio.run(self.sync.check())


def test_a_task_whose_pr_is_open_moves_to_in_review():
    rig = Rig()

    rig.check()

    assert rig.tracker.moves == [("RD-1", "in_review")]


def test_a_draft_pr_does_not_move_its_task():
    rig = Rig()

    rig.check()

    assert ("RD-2", "in_review") not in rig.tracker.moves


def test_merged_closed_and_unlooked_prs_do_not_move_their_tasks():
    rig = Rig()
    rig.fetched["a"] = looked_up(PrState.MERGED)
    rig.fetched["b"] = None  # type: ignore[assignment]

    rig.check()

    assert rig.tracker.moves == []


def test_each_pr_moves_its_task_once_even_across_sweeps_and_restarts():
    rig = Rig()

    rig.check()
    rig.check()
    assert rig.tracker.moves == [("RD-1", "in_review")]  # not asked again in a run

    restarted = Rig()
    restarted.done = rig.done  # what the stack DB remembers
    restarted.check()
    assert restarted.tracker.moves == []  # nor again after a restart


def test_a_task_found_already_in_review_is_recorded_as_done():
    rig = Rig()
    rig.tracker.outcome = "skipped"

    rig.check()

    assert (1, "a", "in_review", 7) in rig.done


def test_a_task_that_could_not_be_moved_is_not_recorded_so_it_is_tried_again_later():
    rig = Rig()
    rig.tracker.outcome = "failed"

    rig.check()

    assert rig.done == set()


def test_a_branch_with_no_task_is_left_alone():
    rig = Rig()
    rig.tasks["a"] = None

    rig.check()

    assert rig.tracker.moves == [] and rig.done == set()


def test_a_tracker_that_raises_does_not_stop_the_sweep():
    rig = Rig()
    rig.fetched["b"] = looked_up(PrState.OPEN, 8)
    calls = []

    def move(ref, stage):
        calls.append(ref)
        if ref == "RD-1":
            raise RuntimeError("boom")
        return StatusMove("moved", "ok")

    rig.tracker.move = move  # type: ignore[method-assign]

    rig.check()

    assert calls == ["RD-1", "RD-2"]
    assert (1, "b", "in_review", 8) in rig.done and (
        1,
        "a",
        "in_review",
        7,
    ) not in rig.done
