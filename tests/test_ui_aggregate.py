"""The aggregate view: replaying a PR's history into states, the numbers built
from them, the GitHub fetch and the endpoint. Pure inputs and an injected
`gh` page runner and clocks: nothing touches GitHub."""

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ui import FakeStatuses

from toolbelt.git.stack.store import (
    PrCandidate,
    PrLink,
    RepoIdentity,
    StackStore,
    default_db_path,
)
from toolbelt.ui import aggregate as agg
from toolbelt.ui import api_models as api
from toolbelt.ui import pr_activity as act
from toolbelt.ui import pr_states
from toolbelt.ui.aggregate import PrRecord, TaskFact
from toolbelt.ui.aggregate_feed import (
    OPEN_SWEEP_INTERVAL,
    AggregateService,
    StoredHistory,
    parse_page,
    parse_pr,
)
from toolbelt.ui.app import create_app
from toolbelt.ui.pr_activity import PrFacts, TimelineItem
from toolbelt.ui.timeline_lanes import LANE_SOURCES

# A Monday, so the window's weeks line up with the test's hours.
MONDAY = datetime(2026, 10, 5, tzinfo=UTC)
VECTORS = Path(__file__).parent.parent / "web" / "src" / "testing" / "phaseVectors.json"


def at(hours: float) -> datetime:
    return MONDAY + timedelta(hours=hours)


def item(
    kind: str,
    hours: float,
    *,
    subject: str = "",
    detail: str = "",
    ci: str | None = None,
    oid: str = "",
    actor: str = "me",
) -> TimelineItem:
    return TimelineItem(at(hours), kind, actor, detail, subject=subject, oid=oid, ci=ci)


def ready(h: float) -> TimelineItem:
    return item(act.READY_FOR_REVIEW, h)


def request(h: float, who: str = "sam") -> TimelineItem:
    return item(act.REVIEW_REQUESTED, h, subject=who)


def review(h: float, detail: str, who: str = "sam") -> TimelineItem:
    return item(act.REVIEW, h, subject=who, actor=who, detail=detail)


def commit(h: float, ci: str | None, oid: str) -> TimelineItem:
    return item(act.COMMIT, h, ci=ci, oid=oid)


def merged(h: float) -> TimelineItem:
    return item(act.MERGED, h)


def pr(
    number: int = 1,
    *,
    created: float = 0,
    merged_at: float | None = None,
    closed_at: float | None = None,
    draft: bool = False,
    items: tuple[TimelineItem, ...] = (),
    head: str | None = None,
    base: str = "main",
    repo: str = "o/api",
    review_now: str = "none",
    base_changes: tuple[tuple[datetime, str, str], ...] = (),
    ci_done: dict[str, datetime] | None = None,
    node_id: str = "",
) -> PrRecord:
    state = "MERGED" if merged_at is not None else "CLOSED" if closed_at else "OPEN"
    facts = PrFacts(
        number=number,
        title=f"PR {number}",
        author="me",
        base_ref=base,
        head_oid="",
        state=state,
        is_draft=draft,
        created_at=at(created),
        merged_at=at(merged_at) if merged_at is not None else None,
        closed_at=at(closed_at) if closed_at is not None else None,
        requested_reviewers=(),
        commit_rollups=(),
        items=tuple(
            sorted(
                (
                    replace(i, ci_done=ci_done[i.oid])
                    if i.oid in (ci_done or {})
                    else i
                    for i in items
                ),
                key=lambda i: i.time,
            )
        ),
    )
    return PrRecord(
        repo=repo,
        head_branch=head or f"branch-{number}",
        facts=facts,
        review_now=review_now,
        base_changes=base_changes,
        node_id=node_id,
    )


def states(record: PrRecord, now_hours: float) -> list[tuple[float, float, str]]:
    return [
        (
            (s.start - MONDAY).total_seconds() / 3600,
            (s.end - MONDAY).total_seconds() / 3600,
            s.state,
        )
        for s in agg.open_segments(record, at(now_hours))
    ]


# ---- parity with the card timeline's phase rules ----------------------------------


_CI_BACK = {"failed": "FAILURE", "success": "SUCCESS", "pending": "PENDING", "": None}


@pytest.mark.parametrize(
    "case", json.loads(VECTORS.read_text())["cases"], ids=lambda c: c["name"]
)
def test_the_phases_match_the_card_timelines_for_the_shared_vectors(case):
    start = MONDAY
    items = tuple(
        TimelineItem(
            time=start + timedelta(minutes=e["t"]),
            kind=e["kind"],
            actor=e["actor"],
            detail=e["detail"],
            subject=e["subject"],
            oid=f"oid{e['t']}",
            ci=_CI_BACK[e["ci"]],
            ci_done=start + timedelta(minutes=e["ciDone"]) if "ciDone" in e else None,
        )
        for e in case["events"]
    )
    facts = PrFacts(
        number=1,
        title="t",
        author="me",
        base_ref="main",
        head_oid="",
        state={"open": "OPEN", "merged": "MERGED", "closed": "CLOSED"}[case["state"]],
        is_draft=case["draft"],
        created_at=start,
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=items,
    )

    spans = agg.phase_spans(
        facts,
        review_now=case["review"],
        start=start + timedelta(minutes=case["start"]),
        end=start + timedelta(minutes=case["end"]),
    )

    got = [
        [kind, (t0 - start).total_seconds() / 60, (t1 - start).total_seconds() / 60]
        for kind, t0, t1 in spans
    ]
    assert got == case["spans"]


# ---- reconstructing a PR's states ---------------------------------------------------------


def test_a_prs_whole_life_draft_to_merged():
    record = pr(
        draft=False,
        merged_at=72,
        items=(
            ready(24),
            request(24, "sam"),
            review(48, "requested changes"),
            commit(50, "FAILURE", "c1"),
            commit(52, "SUCCESS", "c2"),
            review(60, "approved"),
            merged(72),
        ),
    )

    assert states(record, now_hours=100) == [
        (0, 24, "draft"),
        (24, 48, "waiting"),
        # Changes requested outranks CI (the Next rule order), until sam
        # approves.
        (48, 60, "changes"),
        (60, 72, "approved"),
    ]


def test_ci_failing_then_running_then_waiting_for_review():
    record = pr(
        items=(
            request(1),
            commit(2, "FAILURE", "c1"),
            commit(4, "SUCCESS", "c2"),
        ),
        ci_done={"c2": at(5)},
    )

    assert states(record, now_hours=10) == [
        (0, 1, "ready"),
        (1, 2, "waiting"),
        (2, 4, "ci_failing"),
        (4, 5, "ci_running"),
        (5, 10, "waiting"),
    ]


def test_a_failing_commit_waits_on_ci_until_its_checks_complete_then_fails():
    record = pr(
        items=(
            request(1),
            commit(2, "FAILURE", "c1"),
            commit(6, "SUCCESS", "c2"),
        ),
        ci_done={"c1": at(3), "c2": at(7)},
    )

    assert states(record, now_hours=10) == [
        (0, 1, "ready"),
        (1, 2, "waiting"),
        (2, 3, "ci_running"),  # CI took an hour to fail
        (3, 6, "ci_failing"),
        (6, 7, "ci_running"),
        (7, 10, "waiting"),
    ]


def test_a_failed_commit_with_no_completion_time_fails_from_its_push():
    record = pr(items=(commit(2, "FAILURE", "c1"),))

    assert states(record, now_hours=6) == [(0, 2, "ready"), (2, 6, "ci_failing")]


def test_a_commit_that_is_superseded_while_running_stops_running_at_the_push():
    record = pr(
        items=(commit(2, "FAILURE", "c1"), commit(3, "SUCCESS", "c2")),
        ci_done={"c1": at(9), "c2": at(3)},
    )

    assert states(record, now_hours=12) == [
        (0, 2, "ready"),
        (2, 3, "ci_running"),
        (3, 12, "ready"),
    ]


def test_commits_made_before_the_pr_was_opened_set_its_starting_state():
    record = pr(created=5, items=(commit(1, "FAILURE", "c0"), request(6)))

    assert states(record, now_hours=10) == [(5, 10, "ci_failing")]


def test_a_pending_head_commit_is_running_until_the_end():
    record = pr(items=(commit(2, "PENDING", "c1"),))

    assert states(record, now_hours=6) == [(0, 2, "ready"), (2, 6, "ci_running")]


def test_a_force_push_ends_failing_and_the_pr_is_ready_without_a_request():
    record = pr(
        items=(
            commit(1, "ERROR", "c1"),
            item(act.FORCE_PUSH, 3, oid="c1", ci="ERROR"),
        )
    )

    assert states(record, now_hours=5) == [
        (0, 1, "ready"),
        (1, 3, "ci_failing"),
        (3, 5, "ready"),
    ]


def test_a_closed_pr_stops_and_a_reopened_one_resumes():
    record = pr(
        closed_at=None,
        items=(
            request(1),
            item(act.CLOSED, 10),
            item(act.REOPENED, 30),
        ),
    )

    assert states(record, now_hours=40) == [
        (0, 1, "ready"),
        (1, 10, "waiting"),
        (30, 40, "waiting"),
    ]


def test_a_merged_pr_has_no_state_after_the_merge():
    record = pr(merged_at=10, items=(request(2), merged(10)))

    assert states(record, now_hours=50)[-1] == (2, 10, "waiting")


def test_an_approved_child_is_blocked_while_its_parent_is_open_and_who_follows_the_parent():
    # The parent is waiting on a reviewer (others): so is the approved child.
    parent = pr(1, head="parent", items=(request(0),))
    child = pr(
        2,
        head="child",
        base="parent",
        items=(request(1), review(2, "approved")),
        base_changes=((at(30), "parent", "main"),),
    )

    segments = agg.with_blocked(
        [parent, child],
        [agg.open_segments(parent, at(40)), agg.open_segments(child, at(40))],
        at(40),
    )[1]

    assert [(s.state, s.who) for s in segments] == [
        ("ready", "you"),
        ("waiting", "others"),
        ("blocked", "others"),
        # The base became main: no longer blocked, it is yours to merge.
        ("approved", "you"),
    ]
    assert segments[-1].start == at(30)


def test_a_blocked_child_waits_on_you_when_the_parent_does():
    parent = pr(1, head="parent", draft=False, items=(commit(1, "FAILURE", "c"),))
    child = pr(
        2,
        head="child",
        base="parent",
        items=(request(1), review(2, "approved")),
    )

    segments = agg.with_blocked(
        [parent, child],
        [agg.open_segments(parent, at(10)), agg.open_segments(child, at(10))],
        at(10),
    )[1]

    assert segments[-1].state == "blocked"
    assert segments[-1].who == "you"


def test_blocked_stops_when_the_parent_merges():
    parent = pr(1, head="parent", merged_at=5, items=(merged(5),))
    child = pr(
        2,
        head="child",
        base="parent",
        items=(request(1), review(2, "approved")),
    )

    segments = agg.with_blocked(
        [parent, child],
        [agg.open_segments(parent, at(10)), agg.open_segments(child, at(10))],
        at(10),
    )[1]

    assert [(s.state, (s.end - s.start).total_seconds() / 3600) for s in segments] == [
        ("ready", 1),
        ("waiting", 1),
        ("blocked", 3),
        ("approved", 5),
    ]


# ---- matching tasks to PRs -----------------------------------------------------------------


def fact(
    branch: str,
    started: float | None,
    *,
    removed: float | None = None,
    landed: bool = False,
    repo: str | None = "o/api",
    finished: bool = False,
    node_id: str | None = None,
    number: int | None = None,
) -> TaskFact:
    return TaskFact(
        repo=repo,
        branch=branch,
        started_at=at(started) if started is not None else None,
        removed_at=at(removed) if removed is not None else None,
        landed=landed,
        finished=finished,
        pr_node_id=node_id,
        pr_number=number,
    )


def test_a_prs_task_start_is_its_branchs_fact_and_each_fact_is_used_once():
    first = pr(1, created=10, head="feat", closed_at=20)
    second = pr(2, created=30, head="feat")
    facts = [fact("feat", 5, removed=None)]

    tasks = agg.match_tasks([first, second], facts, at(50))

    assert [(t.pr.facts.number, t.started) for t in tasks if t.pr] == [
        (1, at(5)),
        (2, None),
    ]


def test_a_start_after_the_pr_opened_is_ignored_and_does_not_make_a_second_task():
    record = pr(1, created=10, head="feat")

    tasks = agg.match_tasks([record], [fact("feat", 20)], at(50))

    assert [(t.pr is not None, t.started) for t in tasks] == [(True, None)]


def test_an_attempt_removed_before_the_pr_existed_is_not_its_start():
    record = pr(1, created=100, head="feat")
    earlier = fact("feat", 5, removed=50)

    tasks = agg.match_tasks([record], [earlier], at(200))

    assert tasks[0].started is None
    # ...and it is a task of its own: worked on, then abandoned.
    assert (tasks[1].pr, tasks[1].started, tasks[1].ended) == (None, at(5), at(50))


def test_a_recorded_pr_ties_its_task_even_when_the_branch_name_was_reused():
    older = pr(1, created=10, head="feat", closed_at=20, node_id="N1")
    newer = pr(2, created=100, head="feat", node_id="N2")
    # Only the newer task's start was recorded; by name alone the older PR
    # would have taken it (and then dropped it, as it started after the PR).
    facts = [fact("feat", 90, node_id="N2", number=2)]

    tasks = agg.match_tasks([older, newer], facts, at(150))

    assert [(t.pr.facts.number, t.started, t.matched_by) for t in tasks if t.pr] == [
        (1, None, None),
        (2, at(90), "id"),
    ]


def test_each_recorded_task_goes_to_its_own_pr_by_id_not_by_order():
    first = pr(1, created=10, head="feat", closed_at=20, node_id="N1")
    second = pr(2, created=100, head="feat", node_id="N2")
    # Facts in the opposite order, and the earlier one removed late.
    facts = [
        fact("feat", 95, node_id="N2", number=2),
        fact("feat", 5, removed=120, node_id="N1", number=1),
    ]

    tasks = agg.match_tasks([second, first], facts, at(150))

    assert {t.pr.facts.number: t.started for t in tasks if t.pr} == {
        1: at(5),
        2: at(95),
    }


def test_the_number_joins_when_there_is_no_node_id_and_repos_are_kept_apart():
    here = pr(7, created=10, head="x", repo="o/api")
    other = pr(7, created=10, head="y", repo="o/web")

    tasks = agg.match_tasks(
        [here, other], [fact("x", 4, number=7, repo="o/api")], at(50)
    )

    assert {t.pr.head_branch: (t.started, t.matched_by) for t in tasks if t.pr} == {
        "x": (at(4), "id"),
        "y": (None, None),
    }


def test_a_task_that_never_recorded_its_pr_falls_back_to_the_name_and_says_so():
    record = pr(1, created=10, head="feat")

    tasks = agg.match_tasks([record], [fact("feat", 5)], at(50))

    assert [(t.started, t.matched_by) for t in tasks] == [(at(5), "name")]


def test_a_recorded_task_never_matches_another_pr_by_name():
    unrelated = pr(1, created=10, head="feat", node_id="N1")
    fact_of_other = fact("feat", 5, node_id="N9", number=9)

    tasks = agg.match_tasks([unrelated], [fact_of_other], at(50))

    # Its own PR (N9) is not among the fetched ones: not a task without a PR
    # either, and PR 1 gets no start from a name coincidence.
    assert [(t.pr is not None, t.started) for t in tasks] == [(True, None)]


def test_each_pr_says_how_its_task_start_was_matched():
    result = compute(
        [
            pr(1, created=10, head="a", node_id="N1"),
            pr(2, created=11, head="b", node_id="N2"),
            pr(3, created=12, head="c", node_id="N3"),
        ],
        facts=[
            fact("a", 5, node_id="N1", number=1),
            fact("b", 6),
        ],
    )

    assert {
        m.number: (m.matched_by, m.task_started is not None) for m in result.prs
    } == {
        1: ("id", True),
        2: ("name", True),
        3: (None, False),
    }


def test_facts_without_a_pr_are_tasks_unless_landed_or_known_finished():
    tasks = agg.match_tasks(
        [],
        [
            fact("tracked", 1),
            fact("abandoned", 2, removed=9),
            fact("landed-before-window", 3, removed=8, landed=True),
            fact("merged-and-lingering", 4, finished=True),
            fact("no-start", None),
            fact("unknown-repo", 5, repo=None),
        ],
        at(20),
    )

    assert [(t.started, t.ended) for t in tasks if t.pr is None] == [
        (at(1), None),
        (at(2), at(9)),
        (at(5), None),
    ]


# ---- the aggregate ----------------------------------------------------------------------------


DEFAULT_NOW = at(24 * 4)
DEFAULT_SINCE = at(-1000)


def compute(prs, facts=(), *, now=DEFAULT_NOW, weeks=4, since=DEFAULT_SINCE):
    return agg.compute(
        prs,
        list(facts),
        now=now,
        weeks=weeks,
        archive_since=since,
        fetched_at=now,
    )


def test_the_milestones_cover_the_window_and_a_little_more():
    last_week = -24 * 3  # the Friday before MONDAY
    result = compute(
        [
            pr(1, created=last_week),
            pr(2, created=10, merged_at=30, items=(merged(30),)),
            pr(3, created=12, closed_at=40, items=(item(act.CLOSED, 40),)),
            pr(4, created=-24 * 40),  # long before the 4-week window
            pr(5, created=-24 * 40, merged_at=-24 * 39, items=(merged(-24 * 39),)),
        ]
    )

    by_number = {m.number: m for m in result.prs}
    assert sorted(by_number) == [1, 2, 3]
    assert [by_number[n].state for n in (1, 2, 3)] == ["OPEN", "MERGED", "CLOSED"]
    assert by_number[2].opened == "2026-10-05T10:00:00Z"
    assert by_number[2].ended == "2026-10-06T06:00:00Z"
    assert by_number[1].ended is None
    assert by_number[3].ended == "2026-10-06T16:00:00Z"


def _column(mix, instant: datetime) -> dict[str, int]:
    index = max(
        i
        for i, start in enumerate(mix.starts)
        if start <= instant.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return {s.key: s.count[index] for s in mix.series if s.count[index]}


def test_every_sample_sums_to_one_and_has_counts_that_agree():
    record = pr(
        created=24,
        merged_at=24 * 3,
        items=(request(30), review(50, "approved"), merged(24 * 3)),
    )

    mix = compute([record], facts=[fact("branch-1", 12), fact("idea", 1)]).mix

    assert mix.step_hours == 0
    for index in range(len(mix.starts)):
        column = [s.share[index] for s in mix.series]
        counts = [s.count[index] for s in mix.series]
        assert sum(counts) == mix.inflight[index]
        if mix.inflight[index] == 0:
            assert all(v is None for v in column)
        else:
            assert sum(v for v in column if v is not None) == pytest.approx(1)
            for share, count in zip(column, counts):
                assert share == pytest.approx(count / mix.inflight[index])
        who = [s.share[index] for s in mix.who]
        assert sum(s.count[index] for s in mix.who) == mix.inflight[index]
        assert sum(v for v in who if v is not None) == pytest.approx(
            1 if mix.inflight[index] else 0
        )
    assert {s.key for s in mix.series} == {s.key for s in agg.STATES}


def test_an_instant_with_nothing_in_flight_is_a_gap_not_zero():
    # Started Monday 12:00 and removed Tuesday 12:00 (an idea, no PR).
    mix = compute([], facts=[fact("idea", 12, removed=36)], now=at(60)).mix

    working = {s.key: s for s in mix.series}["working"]
    before = mix.starts.index("2026-09-14T00:00:00Z")
    during = mix.starts.index("2026-10-05T12:00:00Z")
    after = mix.starts.index("2026-10-06T12:00:00Z")
    assert working.share[before] is None and mix.inflight[before] == 0
    assert working.share[during] == 1 and working.count[during] == 1
    assert working.share[after] is None


def test_a_sample_is_a_snapshot_of_each_tasks_state_at_that_instant():
    # Task A: PR opened Mon 00:00 as a draft, ready Mon 13:00 (a ready PR
    # with no request). Task B: only working, from Mon 03:00.
    a = pr(1, created=0, draft=True, items=(ready(13),))
    mix = compute([a], facts=[fact("idea", 3)], now=at(30)).mix

    assert _column(mix, at(0)) == {"draft": 1}
    assert _column(mix, at(6)) == {"draft": 1, "working": 1}
    assert _column(mix, at(12)) == {"draft": 1, "working": 1}
    assert _column(mix, at(18)) == {"ready": 1, "working": 1}
    assert "2026-10-05T13:00:00Z" in mix.starts
    index = mix.starts.index("2026-10-05T13:00:00Z")
    shares = {s.key: s.share[index] for s in mix.series}
    assert shares["ready"] == 0.5 and shares["working"] == 0.5


def test_a_task_that_ended_at_the_instant_is_no_longer_in_flight():
    mix = compute([pr(created=0, merged_at=6, items=(merged(6),))], now=at(24)).mix

    assert _column(mix, at(0)) == {"ready": 1}
    assert _column(mix, at(6)) == {}


def test_the_window_includes_its_start_each_transition_and_now():
    now = at(24 * 4 + 5)  # Friday 05:00
    mix = compute([pr(created=0)], now=now, weeks=4).mix

    assert mix.starts[0] == "2026-09-14T00:00:00Z"  # four weeks back, Monday
    assert mix.starts[-1] == "2026-10-09T05:00:00Z"
    assert mix.starts == [
        "2026-09-14T00:00:00Z",
        "2026-10-05T00:00:00Z",
        "2026-10-09T05:00:00Z",
    ]


def test_a_task_that_began_recently_appears_at_its_start():
    mix = compute([pr(created=24 * 4 + 4.5)], now=at(24 * 4 + 5), weeks=4).mix

    assert mix.inflight[-1] == 1
    assert mix.inflight == [0, 1, 1]


def test_the_working_time_before_a_pr_counts_as_in_flight():
    record = pr(created=24, items=(ready(36),))
    mix = compute([record], facts=[fact("branch-1", 12)]).mix

    assert _column(mix, at(12)) == {"working": 1}
    assert _column(mix, at(24)) == {"draft": 1}
    assert _column(mix, at(36)) == {"ready": 1}


def test_who_classes_follow_the_state_rules():
    record = pr(created=0, items=(request(1), commit(2, "PENDING", "c")))
    mix = compute([record], now=at(10), weeks=4).mix
    who = {s.key: s for s in mix.who}
    at_hour = mix.starts.index("2026-10-05T02:00:00Z")

    assert who["ci"].count[at_hour] == 1 and who["ci"].share[at_hour] == 1
    assert who["you"].count[at_hour] == 0
    assert who["others"].count[at_hour] == 0


def test_a_long_window_uses_transitions_too():
    result = compute([pr(created=0)], weeks=26, now=at(24 * 4))

    assert result.mix.step_hours == 0
    assert len(result.mix.starts) == 3


def test_milestones_carry_the_instants_lead_times_are_measured_between():
    complete = pr(
        1,
        created=10,
        merged_at=60,
        head="a",
        items=(request(12), review(20, "commented"), merged(60)),
    )
    no_request = pr(2, created=10, merged_at=40, head="b", items=(merged(40),))
    # The author's own review comment is not "waiting for a review".
    own_comment = pr(
        3,
        created=10,
        merged_at=30,
        head="c",
        items=(review(15, "commented", who="me"), merged(30)),
    )

    result = compute(
        [complete, no_request, own_comment],
        facts=[fact("a", 4), fact("b", 6)],
    )
    m = {x.number: x for x in result.prs}

    def hours(value):
        return (
            None
            if value is None
            else (datetime.fromisoformat(value) - MONDAY).total_seconds() / 3600
        )

    assert hours(m[1].task_started) == 4 and hours(m[1].opened) == 10
    assert hours(m[1].first_review_requested) == 12
    assert hours(m[1].first_review) == 20
    assert hours(m[1].first_review_after_request) == 20
    assert hours(m[1].ended) == 60
    # No recorded start / no request / no review stay None, never guessed.
    assert m[2].task_started == "2026-10-05T06:00:00Z"
    assert m[2].first_review_requested is None and m[2].first_review is None
    assert m[3].task_started is None
    assert m[3].first_review is None


def test_an_open_pr_has_no_end():
    result = compute([pr(1, created=3)])

    [only] = result.prs
    assert (only.state, only.ended, only.task_started) == ("OPEN", None, None)


def test_a_pr_that_ended_long_before_the_window_is_not_sent():
    old = pr(1, created=-24 * 60, merged_at=-24 * 59, items=(merged(-24 * 59),))

    result = compute([old])

    assert result.prs == []


def test_tasks_without_a_pr_are_working_until_removed():
    result = compute([], facts=[fact("idea", 24, removed=48, repo="o/api")], now=at(96))

    assert result.coverage.tasks_without_pr == 1
    mix = result.mix
    assert _column(mix, at(24)) == {"working": 1}
    assert _column(mix, at(42)) == {"working": 1}
    assert _column(mix, at(48)) == {}  # removed: nothing in flight


def test_coverage_says_how_much_has_a_recorded_start():
    result = compute(
        [pr(1, created=10, head="a"), pr(2, created=11, head="b")],
        facts=[fact("a", 5)],
        since=at(-5),
    )

    assert len(result.prs) == 2
    assert result.coverage.task_starts_since == "2026-10-04T19:00:00Z"


def test_the_legend_is_taken_from_the_state_catalog():
    infos = {s.key: s for s in agg.mix_state_infos()}

    assert infos["draft"].label == pr_states.PHASES["draft"].label
    assert infos["approved"].tone == pr_states.PHASES["approved"].tone
    assert infos["working"].legend == pr_states.PHASES["working"].legend
    assert infos["changes"].label == pr_states.lookup("review", "reviewed").label
    assert infos["blocked"].tone == pr_states.lookup("next", "blocked").tone
    assert [s.key for s in agg.STATES] == [i.key for i in agg.mix_state_infos()]


# ---- fetching and the endpoint ------------------------------------------------------------------


def node(
    number: int,
    *,
    repo: str = "o/api",
    head: str | None = None,
    created: str = "2026-10-06T09:00:00Z",
    merged_at: str | None = None,
    state: str = "OPEN",
    decision: str | None = None,
    archived: bool = False,
    items: list[dict] | None = None,
    node_id: str | None = None,
) -> dict:
    return {
        **({"id": node_id} if node_id else {}),
        "number": number,
        "title": f"PR {number}",
        "state": state,
        "isDraft": False,
        "createdAt": created,
        "mergedAt": merged_at,
        "closedAt": merged_at,
        "headRefName": head or f"branch-{number}",
        "baseRefName": "main",
        "reviewDecision": decision,
        "additions": 30,
        "deletions": 7,
        "author": {"login": "me"},
        "repository": {"nameWithOwner": repo, "isArchived": archived},
        "reviewRequests": {"totalCount": 0},
        "timelineItems": {"nodes": items or []},
    }


def page(*nodes: dict, next_cursor: str | None = None, errors: int = 0) -> dict:
    body: dict = {
        "data": {
            "search": {
                "issueCount": len(nodes),
                "pageInfo": {
                    "hasNextPage": next_cursor is not None,
                    "endCursor": next_cursor,
                },
                "nodes": [*nodes, {}],
            }
        }
    }
    if errors:
        body["errors"] = [{"message": "SAML"}] * errors
    return body


def commit_node(oid: str, when: str, state: str, suites: list[dict]) -> dict:
    return {
        "__typename": "PullRequestCommit",
        "commit": {
            "oid": oid,
            "messageHeadline": "work",
            "committedDate": when,
            "author": {"user": {"login": "me"}, "name": "Me"},
            "statusCheckRollup": {"state": state},
            "checkSuites": {"nodes": suites},
        },
    }


def test_a_page_is_parsed_into_pr_records():
    merged_node = node(
        7,
        state="MERGED",
        merged_at="2026-10-08T12:00:00Z",
        decision="APPROVED",
        items=[
            {
                "__typename": "ReviewRequestedEvent",
                "createdAt": "2026-10-06T10:00:00Z",
                "actor": {"login": "me"},
                "requestedReviewer": {"__typename": "User", "login": "sam"},
            },
            commit_node(
                "abc",
                "2026-10-06T11:00:00Z",
                "SUCCESS",
                [
                    {"status": "COMPLETED", "updatedAt": "2026-10-06T11:20:00Z"},
                    {"status": "COMPLETED", "updatedAt": "2026-10-06T11:30:00Z"},
                ],
            ),
            commit_node(
                "def",
                "2026-10-07T11:00:00Z",
                "PENDING",
                [{"status": "IN_PROGRESS", "updatedAt": "2026-10-07T11:01:00Z"}],
            ),
            {
                "__typename": "BaseRefChangedEvent",
                "createdAt": "2026-10-07T09:00:00Z",
                "previousRefName": "parent",
                "currentRefName": "main",
            },
            {
                "__typename": "MergedEvent",
                "createdAt": "2026-10-08T12:00:00Z",
                "actor": {"login": "me"},
            },
            {
                "__typename": "ClosedEvent",
                "createdAt": "2026-10-08T12:00:00Z",
                "actor": {"login": "me"},
            },
        ],
    )

    records, more, cursor, hidden = parse_page(
        page(merged_node, node(8, archived=True), next_cursor="c1", errors=2)
    )

    assert (more, cursor, hidden) == (True, "c1", 2)
    [record] = records  # the archived repo's PR and the empty node are gone
    assert (record.repo, record.facts.number, record.review_now) == (
        "o/api",
        7,
        "approved",
    )
    kinds = [i.kind for i in record.facts.items]
    assert kinds.count(act.MERGED) == 1 and act.CLOSED not in kinds  # merge dedupe
    done = {i.oid: i.ci_done for i in record.facts.items if i.kind == act.COMMIT}
    assert done == {
        "abc": datetime(2026, 10, 6, 11, 30, tzinfo=UTC),
        "def": None,  # still in progress
    }
    assert record.base_changes == (
        (datetime(2026, 10, 7, 9, tzinfo=UTC), "parent", "main"),
    )


class Pages:
    """A canned `gh` page runner: {search kind: [pages]} by the search text."""

    def __init__(self, updated: list[dict], open_now: list[dict] | None = None):
        self.updated = updated
        self.open_now = open_now if open_now is not None else [page()]
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, search: str, after: str | None) -> dict:
        self.calls.append((search, after))
        pages = self.open_now if "is:open" in search else self.updated
        index = 0 if after is None else int(after)
        result = pages[index]
        if isinstance(result, Exception):
            raise result
        return result


NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


async def _no_remote(path: Path) -> str | None:
    return None


def service(pages, history=None, wall_clock=lambda: NOW, **kwargs):
    return AggregateService(
        search=pages,
        history=history
        or (lambda: StoredHistory(repos=[], removed=[], tracked=[], since=at(-100))),
        remote_of=_no_remote,
        finished=lambda repo_id, branch: False,
        wall_clock=wall_clock,
        **kwargs,
    )


class FakeGitHub:
    """A `gh` page runner over a set of PR nodes that answers the two kinds
    of search the service makes (``is:open`` and ``updated:>=``) from their
    ``updatedAt``, like GitHub's, and records what each call returned."""

    def __init__(self, *nodes: dict) -> None:
        self.nodes = {n["number"]: n for n in nodes}
        self.calls: list[str] = []
        #: PR numbers returned by each call, in order.
        self.returned: list[list[int]] = []
        self.page_size = 25

    def put(self, node: dict) -> None:
        self.nodes[node["number"]] = node

    def drop(self, number: int) -> None:
        del self.nodes[number]

    async def __call__(self, search: str, after: str | None) -> dict:
        found = list(self.nodes.values())
        if "is:open" in search:
            found = [n for n in found if n["state"] == "OPEN"]
        match = re.search(r"updated:>=(\S+)", search)
        if match:
            since = match.group(1)
            if "T" not in since:
                since += "T00:00:00Z"
            found = [n for n in found if n["updatedAt"] >= since]
        between = re.search(r"updated:(\d{4}-\d\d-\d\d)\.\.(\d{4}-\d\d-\d\d)", search)
        if between:
            low, high = between.groups()
            found = [
                n
                for n in found
                if f"{low}T00:00:00Z" <= n["updatedAt"] <= f"{high}T23:59:59Z"
            ]
        found.sort(key=lambda n: n["updatedAt"], reverse=True)
        start = int(after or 0)
        chunk = found[start : start + self.page_size]
        more = start + self.page_size < len(found)
        self.calls.append(search)
        self.returned.append([n["number"] for n in chunk])
        return page(*chunk, next_cursor=str(start + self.page_size) if more else None)


def pr_node(number: int, updated: str, **kwargs) -> dict:
    return {**node(number, **kwargs), "updatedAt": updated}


def test_the_first_request_pages_through_the_whole_window_and_dedupes():
    github = FakeGitHub(
        pr_node(1, "2026-10-06T09:00:00Z"),
        pr_node(2, "2026-10-07T09:00:00Z"),
        pr_node(3, "2026-10-08T09:00:00Z"),
    )
    github.page_size = 2
    svc = service(github)

    result = asyncio.run(svc.get(12))

    assert sorted(m.number for m in result.prs) == [1, 2, 3]  # once each
    assert result.refreshed == 3
    assert any("updated:>=2026-07-19" in call for call in github.calls)
    assert all("author:@me" in c and "archived:false" in c for c in github.calls)
    assert [len(r) for r in github.returned] == [2, 1, 2, 1]  # 2 searches, 2 pages


def test_a_later_request_re_fetches_only_what_github_says_changed():
    old_merged = pr_node(
        1,
        "2026-09-20T09:00:00Z",
        state="MERGED",
        merged_at="2026-09-20T09:00:00Z",
        created="2026-09-19T09:00:00Z",
    )
    github = FakeGitHub(
        old_merged,
        pr_node(2, "2026-10-01T09:00:00Z"),
        pr_node(3, "2026-10-07T09:00:00Z"),
        pr_node(
            4,
            "2026-10-08T11:00:00Z",
            state="MERGED",
            merged_at="2026-10-08T11:00:00Z",
        ),
    )
    svc = service(github)
    asyncio.run(svc.get(12))
    github.calls.clear()
    github.returned.clear()

    result = asyncio.run(svc.get(12))

    # Only what changed since the newest one held (4): the open PRs were all
    # fetched a moment ago, so they are not fetched again.
    assert github.returned == [[4]]
    assert result.refreshed == 1
    assert len(result.prs) == 4
    # newest - 5 min
    assert github.calls == [
        "is:pr author:@me archived:false updated:>=2026-10-08T10:55:00Z sort:updated-desc"
    ]


def test_every_open_pr_is_fetched_again_once_the_sweep_interval_has_passed():
    github = FakeGitHub(
        pr_node(1, "2026-10-01T09:00:00Z"),
        pr_node(2, "2026-10-07T09:00:00Z"),
    )
    clock = [NOW]
    svc = service(github, wall_clock=lambda: clock[0])
    asyncio.run(svc.get(12))
    github.calls.clear()
    github.returned.clear()
    clock[0] = NOW + OPEN_SWEEP_INTERVAL

    asyncio.run(svc.get(12))

    assert sorted(n for returned in github.returned for n in returned) == [1, 2, 2]
    assert any("is:open" in call for call in github.calls)


def test_a_cached_request_does_not_wait_for_github_refresh():
    github = FakeGitHub(pr_node(1, "2026-10-06T09:00:00Z"))
    svc = service(github)
    first = asyncio.run(svc.get(52))
    github.calls.clear()

    cached = asyncio.run(svc.get(4, refresh=False))

    assert github.calls == []
    assert cached.fetched_at == first.fetched_at
    assert [pr_.number for pr_ in cached.prs] == [1]


def test_background_warms_twelve_weeks_without_fetching_fifty_two():
    github = FakeGitHub(pr_node(1, "2026-10-06T09:00:00Z"))
    svc = service(github)

    async def check() -> None:
        running = asyncio.create_task(svc.run(interval=3600))
        try:
            while svc._cache is None:
                await asyncio.sleep(0)
            assert svc._cache.covered_from.date().isoformat() == "2026-07-19"
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(asyncio.wait_for(check(), timeout=2))
    assert any("updated:>=2026-07-19" in call for call in github.calls)


def test_a_changed_pr_replaces_its_cached_record_and_deletions_are_dropped():
    github = FakeGitHub(
        pr_node(1, "2026-10-06T09:00:00Z"),
        pr_node(2, "2026-10-06T10:00:00Z"),
    )
    clock = [NOW]
    svc = service(github, wall_clock=lambda: clock[0])
    first = asyncio.run(svc.get(12))
    assert [m.state for m in first.prs] == ["OPEN", "OPEN"]

    github.put(
        pr_node(
            1,
            "2026-10-07T09:00:00Z",
            state="MERGED",
            merged_at="2026-10-07T09:00:00Z",
        )
    )
    github.drop(2)  # deleted / transferred away
    # An open PR that GitHub no longer reports is only noticed by a sweep.
    clock[0] = NOW + OPEN_SWEEP_INTERVAL
    result = asyncio.run(svc.get(12))

    assert [(m.number, m.state) for m in result.prs] == [(1, "MERGED")]  # 2 is gone


def test_a_closed_pr_that_is_reopened_is_open_again():
    github = FakeGitHub(
        pr_node(
            1,
            "2026-10-06T09:00:00Z",
            state="CLOSED",
            merged_at="2026-10-06T09:00:00Z",
        )
    )
    svc = service(github)
    closed = asyncio.run(svc.get(12))
    assert [m.state for m in closed.prs] == ["CLOSED"]

    github.put(pr_node(1, "2026-10-08T09:00:00Z"))
    reopened = asyncio.run(svc.get(12))

    assert [m.state for m in reopened.prs] == ["OPEN"]
    assert github.returned[-1] == [1]  # found by the updated search


def test_every_answer_is_as_fresh_as_its_request():
    github = FakeGitHub(pr_node(1, "2026-10-06T09:00:00Z"))
    clock = [datetime(2026, 10, 8, 12, 0, tzinfo=UTC)]
    svc = service(github, wall_clock=lambda: clock[0])

    first = asyncio.run(svc.get(12))
    clock[0] = datetime(2026, 10, 8, 12, 0, 5, tzinfo=UTC)
    second = asyncio.run(svc.get(12))

    assert (first.fetched_at, second.fetched_at) == (
        "2026-10-08T12:00:00Z",
        "2026-10-08T12:00:05Z",
    )
    assert len(github.calls) == 3  # the second request asked GitHub again, for changes


def test_a_wider_window_fetches_only_the_older_part_and_a_narrower_one_is_served():
    github = FakeGitHub(
        pr_node(1, "2026-10-06T09:00:00Z"),
        pr_node(
            2, "2026-05-02T09:00:00Z", state="MERGED", merged_at="2026-05-02T09:00:00Z"
        ),
    )
    svc = service(github)

    asyncio.run(svc.get(4))
    github.calls.clear()
    github.returned.clear()
    wide = asyncio.run(svc.get(26))

    # Only PRs updated before the stored part, which began 4 weeks (+1 day) back.
    assert len(github.calls) == 1
    assert re.search(r"updated:2026-04-\d\d\.\.2026-09-13", github.calls[0])
    assert github.returned == [[2]]
    assert sorted(m.number for m in wide.prs) == [1, 2]
    github.calls.clear()
    narrow = asyncio.run(svc.get(4))

    assert wide.weeks == 26 and narrow.weeks == 4
    # The narrow one asks for changes only: no whole-window query.
    assert (
        len(github.calls) == 1 and "updated:>=2026-10-06T08:55:00Z" in github.calls[0]
    )


def test_each_pr_is_stored_once_whatever_windows_asked_for_it():
    github = FakeGitHub(
        pr_node(1, "2026-10-06T09:00:00Z"),
        pr_node(
            2, "2026-05-02T09:00:00Z", state="MERGED", merged_at="2026-05-02T09:00:00Z"
        ),
    )
    svc = service(github)

    for weeks in (4, 12, 26, 52, 4):
        asyncio.run(svc.get(weeks))

    store = StackStore(default_db_path())
    try:
        rows = store.aggregate_prs()
        sync = store.aggregate_sync()
    finally:
        store.close()
    assert sorted((r.repo, r.number) for r in rows) == [("o/api", 1), ("o/api", 2)]
    assert sync is not None and sync.covered_from < datetime(2025, 10, 14, tzinfo=UTC)


def test_a_restart_reads_the_stored_history_and_asks_github_only_for_changes():
    github = FakeGitHub(
        pr_node(1, "2026-10-06T09:00:00Z"),
        pr_node(2, "2026-10-07T09:00:00Z"),
    )
    first = asyncio.run(service(github).get(12))
    github.calls.clear()
    github.returned.clear()

    restarted = service(github)
    served = asyncio.run(restarted.get(12, refresh=False))

    assert github.calls == []  # served from the DB, as of the first fetch
    assert sorted(m.number for m in served.prs) == [1, 2]
    assert served.fetched_at == first.fetched_at

    github.put(
        pr_node(
            2, "2026-10-08T10:00:00Z", state="MERGED", merged_at="2026-10-08T10:00:00Z"
        )
    )
    refreshed = asyncio.run(restarted.get(12))

    # Not the whole window again, and not the open PRs: what changed.
    assert github.returned == [[2]]
    assert sorted((m.number, m.state) for m in refreshed.prs) == [
        (1, "OPEN"),
        (2, "MERGED"),
    ]


def test_stored_history_survives_the_json_round_trip_with_its_timeline():
    from toolbelt.ui.aggregate_feed import record_from_json, record_to_json

    record = parse_pr(
        {
            **pr_node(
                7,
                "2026-10-06T09:00:00Z",
                state="MERGED",
                merged_at="2026-10-06T12:00:00Z",
                node_id="PR_7",
            ),
            "timelineItems": {
                "nodes": [
                    {
                        "__typename": "ReviewRequestedEvent",
                        "createdAt": "2026-10-06T10:00:00Z",
                        "actor": {"login": "me"},
                        "requestedReviewer": {"__typename": "User", "login": "sam"},
                    },
                    {
                        "__typename": "MergedEvent",
                        "createdAt": "2026-10-06T12:00:00Z",
                        "actor": {"login": "me"},
                    },
                    {
                        "__typename": "BaseRefChangedEvent",
                        "createdAt": "2026-10-06T11:00:00Z",
                        "previousRefName": "devon/a",
                        "currentRefName": "main",
                    },
                ]
            },
        }
    )

    assert record_from_json(record_to_json(record)) == record


def test_history_written_by_another_schema_is_dropped_and_fetched_again():
    github = FakeGitHub(pr_node(1, "2026-10-06T09:00:00Z"))
    asyncio.run(service(github).get(12))
    store = StackStore(default_db_path())
    try:
        sync = store.aggregate_sync()
        assert sync is not None
        store.save_aggregate(
            changed=[], removed=[], sync=replace(sync, schema=sync.schema + 1)
        )
    finally:
        store.close()
    github.calls.clear()

    asyncio.run(service(github).get(12))

    assert any("updated:>=2026-07-19" in call for call in github.calls)  # whole window


def test_the_updated_search_is_paged_too():
    github = FakeGitHub(*(pr_node(n, f"2026-10-0{n}T09:00:00Z") for n in range(1, 7)))
    github.page_size = 2
    svc = service(github)
    asyncio.run(svc.get(12))
    github.calls.clear()
    github.returned.clear()
    for n in range(1, 7):  # all of them changed
        github.put(pr_node(n, f"2026-10-08T0{n}:00:00Z"))

    result = asyncio.run(svc.get(12))

    assert result.refreshed == 6


def test_a_failure_after_the_first_page_gives_a_partial_flagged_result():
    pages = Pages(
        updated=[page(node(1), next_cursor="1"), RuntimeError("rate limited")],
    )

    result = asyncio.run(service(pages).get(12))

    assert result.coverage.truncated
    assert len(result.prs) == 1


def test_a_failure_of_the_first_page_propagates():
    pages = Pages(updated=[RuntimeError("no auth")])

    with pytest.raises(RuntimeError, match="no auth"):
        asyncio.run(service(pages).get(12))


def test_unauthorized_organizations_are_counted_as_hidden():
    pages = Pages(updated=[page(node(1), errors=2)])

    result = asyncio.run(service(pages).get(12))

    assert result.coverage.hidden == 2


def test_stored_task_starts_reach_the_prs_through_the_repo_remote():
    store = StackStore(default_db_path())
    try:
        repo_id = store.ensure_repo(
            RepoIdentity(
                git_common_dir=Path("/r/.git"),
                path=Path("/r"),
                remote_url="git@github.com:O/Api.git",
            )
        )
        store.set_parent(
            repo_id,
            "branch-1",
            "main",
            started_at=datetime(2026, 10, 5, 8, tzinfo=UTC),
        )
        store.set_parent(
            repo_id,
            "branch-2",
            "main",
            started_at=datetime(2026, 10, 6, 1, tzinfo=UTC),
        )
        store.remove_branch(repo_id, "branch-2", landed=True)
    finally:
        store.close()
    from toolbelt.ui.aggregate_feed import read_history

    pages = Pages(
        updated=[
            page(
                node(1, created="2026-10-06T09:00:00Z"),
                node(2, state="MERGED", merged_at="2026-10-07T09:00:00Z"),
            )
        ]
    )

    result = asyncio.run(service(pages, history=read_history).get(12))

    started = {m.number: m.task_started for m in result.prs}
    assert started == {1: "2026-10-05T08:00:00Z", 2: "2026-10-06T01:00:00Z"}
    assert result.coverage.task_starts_since.endswith("Z")


def test_recorded_pr_ids_tie_stored_tasks_to_prs_even_when_a_name_was_reused():
    store = StackStore(default_db_path())
    try:
        repo_id = store.ensure_repo(
            RepoIdentity(
                git_common_dir=Path("/r/.git"),
                path=Path("/r"),
                remote_url="git@github.com:O/Api.git",
            )
        )
        # The first task of the name: its PR (#1) was recorded, then removed.
        store.set_parent(
            repo_id, "reused", "main", started_at=datetime(2026, 10, 1, 8, tzinfo=UTC)
        )
        store.link_pr(
            repo_id,
            "reused",
            [PrCandidate(PrLink(1, "N1"), datetime(2026, 10, 1, 9, tzinfo=UTC))],
        )
        store.remove_branch(repo_id, "reused", landed=True)
        # The same name again, for PR #2.
        store.set_parent(
            repo_id, "reused", "main", started_at=datetime(2026, 10, 6, 1, tzinfo=UTC)
        )
        store.link_pr(
            repo_id,
            "reused",
            [PrCandidate(PrLink(2, "N2"), datetime.now(UTC) + timedelta(days=1))],
        )
    finally:
        store.close()
    from toolbelt.ui.aggregate_feed import read_history

    pages = Pages(
        updated=[
            page(
                node(
                    1,
                    head="reused",
                    node_id="N1",
                    created="2026-10-01T09:00:00Z",
                    state="MERGED",
                    merged_at="2026-10-02T09:00:00Z",
                ),
                node(
                    2,
                    head="reused",
                    node_id="N2",
                    created="2026-10-06T09:00:00Z",
                ),
            )
        ]
    )

    result = asyncio.run(service(pages, history=read_history).get(12))

    by_number = {m.number: m for m in result.prs}
    # Each PR got its own task's start though both are branch "reused".
    assert by_number[1].task_started == "2026-10-01T08:00:00Z"
    assert by_number[2].task_started == "2026-10-06T01:00:00Z"
    assert {m.matched_by for m in result.prs} == {"id"}


def _two_repos():
    store = StackStore(default_db_path())
    try:
        api_id = store.ensure_repo(
            RepoIdentity(
                git_common_dir=Path("/api/.git"),
                path=Path("/api"),
                remote_url="git@github.com:O/Api.git",
            )
        )
        web_id = store.ensure_repo(
            RepoIdentity(
                git_common_dir=Path("/web/.git"),
                path=Path("/web"),
                remote_url="git@github.com:O/Web.git",
            )
        )
        store.set_parent(
            api_id, "branch-1", "main", started_at=datetime(2026, 10, 5, 8, tzinfo=UTC)
        )
        store.set_parent(
            web_id, "branch-2", "main", started_at=datetime(2026, 10, 5, 8, tzinfo=UTC)
        )
        store.set_parent(
            web_id, "idea", "main", started_at=datetime(2026, 10, 6, 8, tzinfo=UTC)
        )
    finally:
        store.close()
    return api_id, web_id


def test_the_repo_filter_narrows_prs_and_tasks_to_one_repo():
    _api_id, web_id = _two_repos()
    from toolbelt.ui.aggregate_feed import read_history

    github = FakeGitHub(
        pr_node(1, "2026-10-06T09:00:00Z", repo="o/api"),
        pr_node(2, "2026-10-06T10:00:00Z", repo="o/web"),
        pr_node(3, "2026-10-06T11:00:00Z", repo="o/web"),
    )
    svc = service(github, history=read_history)

    everything = asyncio.run(svc.get(12))
    by_id = asyncio.run(svc.get(12, str(web_id)))
    by_name = asyncio.run(svc.get(12, "API"))

    assert (everything.repo, len(everything.prs)) == (None, 3)
    assert by_id.repo == "web"
    assert sorted((m.number, m.task_started is not None) for m in by_id.prs) == [
        (2, True),
        (3, False),
    ]
    assert (by_name.repo, [m.number for m in by_name.prs]) == ("api", [1])
    # The task without a PR of the other repo is not counted for this one.
    assert by_name.coverage.tasks_without_pr == 0
    assert by_id.coverage.tasks_without_pr == 1
    assert max(by_name.mix.inflight) == 1
    assert len(github.calls) > 0


def test_an_unknown_repo_is_a_lookup_error():
    github = FakeGitHub(pr_node(1, "2026-10-06T09:00:00Z"))

    with pytest.raises(LookupError, match="nope"):
        asyncio.run(service(github).get(12, "nope"))


@pytest.fixture
def client_and_pages(tmp_path: Path):
    pages = Pages(
        updated=[
            page(
                node(
                    1,
                    state="MERGED",
                    merged_at="2026-10-07T09:00:00Z",
                    created="2026-10-06T09:00:00Z",
                )
            )
        ]
    )

    async def remote_of(path: Path) -> str | None:
        return None

    app = create_app(
        None,
        fetch=FakeStatuses(),
        tracker=lambda: None,
        wall_clock=lambda: NOW,
        search_history=pages,
        remote_of=remote_of,
    )
    return TestClient(app), pages


def test_the_aggregate_endpoint_serves_the_computed_view(client_and_pages):
    client, _pages = client_and_pages

    body = client.get("/api/aggregate?weeks=4").json()

    assert body["weeks"] == 4
    assert body["window_end"] == "2026-10-08T12:00:00Z"
    [pr_] = body["prs"]
    assert (pr_["number"], pr_["state"]) == (1, "MERGED")
    assert pr_["opened"] == "2026-10-06T09:00:00Z"
    assert pr_["ended"] == "2026-10-07T09:00:00Z"
    assert body["mix"]["step_hours"] == 0
    assert body["mix"]["states"][0]["label"] == pr_states.PHASES["working"].label
    assert body["notes"]


def test_the_endpoint_passes_the_repo_filter_and_reports_an_unknown_one(
    client_and_pages,
):
    client, _ = client_and_pages

    assert client.get("/api/aggregate?repo=nope").status_code == 404


def test_the_endpoint_defaults_to_twelve_weeks_and_rejects_other_windows(
    client_and_pages,
):
    client, _ = client_and_pages

    assert client.get("/api/aggregate").json()["weeks"] == 12
    assert client.get("/api/aggregate?weeks=7").status_code == 400


def test_the_aggregate_is_not_fetched_by_the_snapshot_or_at_startup(client_and_pages):
    client, pages = client_and_pages

    client.get("/api/snapshot")

    assert pages.calls == []


def test_a_prs_lines_added_and_deleted_reach_its_milestones():
    from toolbelt.ui.aggregate_feed import parse_pr

    record = parse_pr(node(5, state="MERGED", merged_at="2026-10-07T09:00:00Z"))

    assert (record.facts.additions, record.facts.deletions) == (30, 7)

    result = compute([record], facts=[])

    assert [(m.additions, m.deletions) for m in result.prs] == [(30, 7)]


def test_a_node_without_line_counts_has_zero():
    from toolbelt.ui.aggregate_feed import parse_pr

    bare = node(6)
    del bare["additions"], bare["deletions"]

    record = parse_pr(bare)

    assert (record.facts.additions, record.facts.deletions) == (0, 0)


# ---- claims and lane layers ----------------------------------------------------------------


def lane_phase(kind: str, start: float, end: float | None) -> api.LanePhase:
    return api.LanePhase.model_validate(
        {
            "start": at(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": at(end).strftime("%Y-%m-%dT%H:%M:%SZ") if end is not None else None,
            "kind": kind,
            "detail": "",
        }
    )


def lanes_of(lane: str, *phases: api.LanePhase) -> tuple[api.TimelineLane, ...]:
    return (api.TimelineLane(lane=lane, events=[], phases=list(phases)),)  # type: ignore[arg-type]


def claimed_task(*phases: api.LanePhase) -> TaskFact:
    return replace(
        fact("branch-1", None, node_id="n1", number=1),
        lanes=lanes_of("claim", *phases),
    )


def test_a_claim_takes_the_task_from_its_prs_state_while_it_is_held():
    # A ready PR from Mon 00:00, claimed from 06:00 to 12:00.
    mix = compute(
        [pr(1, created=0, node_id="n1")],
        facts=[claimed_task(lane_phase("claim_agent_worktree", 6, 12))],
        now=at(30),
    ).mix

    assert _column(mix, at(3)) == {"ready": 1}
    assert _column(mix, at(8)) == {"agent": 1}
    assert _column(mix, at(18)) == {"ready": 1}
    who = {w.key: w.count[mix.starts.index("2026-10-05T06:00:00Z")] for w in mix.who}
    assert who["agent"] == 1 and who["you"] == 0


def test_a_claim_still_held_runs_to_now():
    mix = compute(
        [pr(1, created=0, node_id="n1")],
        facts=[claimed_task(lane_phase("claim_autonomous_ref", 6, None))],
        now=at(30),
    ).mix

    assert _column(mix, at(30)) == {"agent": 1}


def test_overlapping_claims_are_one_interval():
    claimed = agg.claimed_intervals(
        lanes_of(
            "claim",
            lane_phase("claim_agent_worktree", 1, 5),
            lane_phase("claim_human_ref", 4, 8),
            lane_phase("claim_agent_ref", 10, 11),
        ),
        at(20),
    )

    assert claimed == [(at(1), at(8)), (at(10), at(11))]


def test_a_lane_layer_counts_the_tasks_in_flight_that_had_its_span():
    # Two tasks in flight; the first conflicts from 06:00 to 12:00.
    conflicting = replace(
        fact("branch-1", None, node_id="n1", number=1),
        lanes=lanes_of("git", lane_phase("git_conflicting", 6, 12)),
    )
    result = compute(
        [pr(1, created=0, node_id="n1"), pr(2, created=0)],
        facts=[conflicting],
        now=at(30),
    )

    (git,) = [lane for lane in result.lanes if lane.lane == "git"]
    (phase,) = git.phases
    inside = result.mix.starts.index("2026-10-05T06:00:00Z")
    after = result.mix.starts.index("2026-10-05T12:00:00Z")
    assert (phase.count[inside], phase.share[inside]) == (1, 0.5)
    assert phase.count[after] == 0


def test_a_lane_layer_lists_each_markers_times_in_the_window():
    posted = replace(
        fact("branch-1", None, node_id="n1", number=1),
        lanes=(
            api.TimelineLane(
                lane="slack",
                phases=[],
                events=[
                    api.LaneEvent(
                        t="2026-10-05T09:00:00Z", kind="slack_post", detail=""
                    ),
                    api.LaneEvent(
                        t="2026-09-01T09:00:00Z", kind="slack_bump", detail=""
                    ),
                ],
            ),
        ),
    )
    result = compute([pr(1, created=0, node_id="n1")], facts=[posted], now=at(30))

    (slack,) = [lane for lane in result.lanes if lane.lane == "slack"]
    times = {e.kind: e.times for e in slack.events}
    assert times == {
        "slack_post": ["2026-10-05T09:00:00Z"],
        "slack_bump": [],
        "slack_reassign": [],
    }


def test_the_lanes_the_aggregate_skips_say_why_and_the_rest_have_a_layer():
    result = compute([pr(1, created=0)])

    skipped = {s.id for s in LANE_SOURCES if s.aggregate_skip}
    assert skipped == {"ci"}
    assert [lane.lane for lane in result.lanes] == [
        s.id for s in LANE_SOURCES if s.id not in skipped
    ]
