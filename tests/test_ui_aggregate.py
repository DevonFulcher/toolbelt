"""The aggregate view: replaying a PR's history into states, the numbers built
from them, the GitHub fetch and the endpoint. Pure inputs and an injected
`gh` page runner and clocks: nothing touches GitHub."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolbelt.git.stack.store import (
    PrCandidate,
    PrLink,
    RepoIdentity,
    StackStore,
    default_db_path,
)
from toolbelt.ui import aggregate as agg
from toolbelt.ui import pr_activity as act
from toolbelt.ui import pr_states
from toolbelt.ui.aggregate import PrRecord, TaskFact
from toolbelt.ui.aggregate_feed import (
    AggregateService,
    StoredHistory,
    parse_page,
)
from toolbelt.ui.app import create_app
from toolbelt.ui.pr_activity import PrFacts, TimelineItem

from test_ui import FakeStatuses

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
        items=tuple(sorted(items, key=lambda i: i.time)),
    )
    return PrRecord(
        repo=repo,
        head_branch=head or f"branch-{number}",
        facts=facts,
        review_now=review_now,
        base_changes=base_changes,
        ci_done=ci_done or {},
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


def test_coverage_counts_the_starts_matched_by_name():
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

    assert result.coverage.prs_with_start == 2
    assert result.coverage.starts_by_name == 1


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


def compute(prs, facts=(), *, now=at(24 * 4), weeks=4, since=at(-1000)):
    return agg.compute(
        prs,
        list(facts),
        now=now,
        weeks=weeks,
        archive_since=since,
        fetched_at=now,
    )


def test_throughput_counts_prs_per_week_across_the_window():
    last_week = -24 * 3  # the Friday before MONDAY
    result = compute(
        [
            pr(1, created=last_week),
            pr(2, created=10, merged_at=30, items=(merged(30),)),
            pr(3, created=12, closed_at=40, items=(item(act.CLOSED, 40),)),
            pr(4, created=-24 * 40),  # before the 4-week window
        ]
    )

    t = result.throughput
    assert len(t.starts) == 4
    assert t.starts[-1] == "2026-10-05T00:00:00Z"
    assert t.opened[-2:] == [1, 2]
    assert t.merged == [0, 0, 0, 1]
    assert t.closed == [0, 0, 0, 1]


def test_each_status_mix_bucket_sums_to_one_and_empty_ones_are_none():
    record = pr(
        created=24,
        merged_at=24 * 3,
        items=(request(30), review(50, "approved"), merged(24 * 3)),
    )

    mix = compute([record], facts=[fact("branch-1", 12)]).mix

    assert mix.granularity == "day"
    count = len(mix.starts)
    for index in range(count):
        column = [s.share[index] for s in mix.series]
        if all(v is None for v in column):
            assert mix.inflight[index] == 0
        else:
            assert sum(v for v in column if v is not None) == pytest.approx(1)
        who = [s.share[index] for s in mix.who]
        assert (sum(v for v in who if v is not None) or 1) == pytest.approx(1)
    # Before the task started nothing was in flight.
    assert all(s.share[0] is None for s in mix.series)
    assert {s.key for s in mix.series} == {s.key for s in agg.STATES}


def test_time_in_each_state_is_weighted_by_duration():
    # Started Monday 12:00; the PR opened Tuesday 00:00 as a draft and was
    # marked ready at 12:00. Monday has 12h of working only; Tuesday has 12h
    # of draft and 12h of ready.
    record = pr(created=24, items=(ready(36),))
    mix = compute([record], facts=[fact("branch-1", 12)]).mix
    monday = next(i for i, s in enumerate(mix.starts) if s == "2026-10-05T00:00:00Z")
    tuesday = monday + 1
    share = {s.key: s.share for s in mix.series}

    assert share["working"][monday] == pytest.approx(1.0)
    assert share["working"][tuesday] == 0
    assert share["draft"][tuesday] == pytest.approx(0.5)
    assert share["ready"][tuesday] == pytest.approx(0.5)
    assert mix.inflight[monday] == pytest.approx(0.5)
    assert mix.inflight[tuesday] == pytest.approx(1.0)


def test_who_classes_follow_the_state_rules():
    record = pr(created=0, items=(request(1), commit(2, "PENDING", "c")))
    mix = compute([record], now=at(10), weeks=4).mix
    shares = {s.key: s.share for s in mix.who}
    today = len(mix.starts) - 1

    # 10h of the day: 1h you (ready), 1h others (waiting), 8h CI.
    assert shares["you"][today] == pytest.approx(0.1)
    assert shares["others"][today] == pytest.approx(0.1)
    assert shares["ci"][today] == pytest.approx(0.8)


def test_weekly_buckets_beyond_twelve_weeks():
    result = compute([pr(created=0)], weeks=26, now=at(24 * 4))

    assert result.mix.granularity == "week"
    assert len(result.mix.starts) == 26


def test_lead_time_stats_and_unknown_intervals():
    complete = pr(
        1,
        created=10,
        merged_at=60,
        head="a",
        items=(request(12), review(20, "commented"), merged(60)),
    )
    no_request = pr(2, created=10, merged_at=40, head="b", items=(merged(40),))
    # An unrequested review of its own is not "waiting for a review".
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
    stages = {s.key: s for s in result.lead_time}

    # started -> opened: only the PRs with a recorded start (6h and 4h).
    assert stages["start_to_open"].values_s == [4 * 3600, 6 * 3600]
    assert stages["start_to_open"].unknown == 1
    assert stages["start_to_open"].median_s == 5 * 3600
    # opened -> requested: only `complete` has a request (2h).
    assert stages["open_to_request"].values_s == [2 * 3600]
    assert stages["open_to_request"].unknown == 2
    # requested -> first review: 8h.
    assert stages["request_to_review"].values_s == [8 * 3600]
    # first review -> merged: complete 40h; the author's own comment is no
    # review, so the others are unknown.
    assert stages["review_to_merge"].values_s == [40 * 3600]
    assert stages["review_to_merge"].unknown == 2
    # started -> merged: a: 56h, b: 34h.
    assert stages["start_to_merge"].values_s == [34 * 3600, 56 * 3600]
    assert stages["start_to_merge"].p90_s == pytest.approx(
        (34 + (56 - 34) * 0.9) * 3600
    )


def test_a_pr_with_no_merged_prs_has_empty_stats_not_invented_ones():
    result = compute([pr(1, created=3)])

    for stage in result.lead_time:
        assert (stage.n, stage.median_s, stage.values_s) == (0, None, [])


def test_lead_time_only_counts_prs_merged_inside_the_window():
    old = pr(1, created=-24 * 60, merged_at=-24 * 59, items=(merged(-24 * 59),))

    result = compute([old])

    assert {s.n for s in result.lead_time} == {0}


def test_tasks_without_a_pr_are_working_until_removed():
    result = compute([], facts=[fact("idea", 24, removed=48, repo="o/api")], now=at(96))

    assert result.coverage.tasks_without_pr == 1
    share = {s.key: s.share for s in result.mix.series}["working"]
    monday = next(
        i for i, s in enumerate(result.mix.starts) if s == "2026-10-06T00:00:00Z"
    )
    assert share[monday] == pytest.approx(1.0)
    assert share[monday + 2] is None  # removed: nothing in flight


def test_coverage_says_how_much_has_a_recorded_start():
    result = compute(
        [pr(1, created=10, head="a"), pr(2, created=11, head="b")],
        facts=[fact("a", 5)],
        since=at(-5),
    )

    assert (result.coverage.prs, result.coverage.prs_with_start) == (2, 1)
    assert result.coverage.task_starts_since == "2026-10-04T19:00:00Z"


def test_the_legend_is_taken_from_the_state_catalog():
    infos = {s.key: s for s in agg.mix_state_infos()}

    assert infos["draft"].label == pr_states.PHASES["draft"].label
    assert infos["approved"].tone == pr_states.PHASES["approved"].tone
    assert infos["working"].legend == pr_states.PHASES["working"].legend
    assert infos["changes"].label == pr_states.lookup("review", "reviewed").label
    assert infos["blocked"].tone == pr_states.lookup("next", "blocked").tone
    assert [s.key for s in agg.STATES] == [i.key for i in agg.mix_state_infos()]


def test_percentile_interpolates():
    assert agg.percentile([], 0.5) is None
    assert agg.percentile([7.0], 0.9) == 7.0
    assert agg.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert agg.percentile([0.0, 10.0], 0.75) == 7.5


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
    assert record.ci_done == {"abc": datetime(2026, 10, 6, 11, 30, tzinfo=UTC)}
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


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


async def _no_remote(path: Path) -> str | None:
    return None


def service(pages: Pages, clock: Clock | None = None, history=None, **kwargs):
    return AggregateService(
        search=pages,
        history=history
        or (lambda: StoredHistory(repos=[], removed=[], tracked=[], since=at(-100))),
        remote_of=_no_remote,
        finished=lambda repo_id, branch: False,
        clock=clock or Clock(),
        wall_clock=lambda: NOW,
        **kwargs,
    )


def test_both_searches_are_paged_deduplicated_and_cached():
    pages = Pages(
        updated=[
            page(node(1), node(2), next_cursor="1"),
            page(node(3)),
        ],
        open_now=[page(node(2), node(4))],
    )
    clock = Clock()
    svc = service(pages, clock, ttl=900)

    first = asyncio.run(svc.get(12))
    calls = len(pages.calls)
    asyncio.run(svc.get(4))  # a smaller window is served from the same fetch
    clock.now += 899
    asyncio.run(svc.get(12))

    assert first.throughput.opened[-1] == 4  # 1, 2, 3, 4 once each
    assert calls == 3 and len(pages.calls) == 3
    assert any("updated:>=2026-07-19" in call[0] for call in pages.calls)
    assert all(
        "author:@me" in call[0] and "archived:false" in call[0] for call in pages.calls
    )
    clock.now += 2
    asyncio.run(svc.get(12))
    assert len(pages.calls) == 6  # expired: fetched again


def test_a_larger_window_than_the_cached_one_fetches_again():
    pages = Pages(updated=[page(node(1))])
    svc = service(pages)

    asyncio.run(svc.get(4))
    asyncio.run(svc.get(26))

    assert len(pages.calls) == 4


def test_a_failure_after_the_first_page_gives_a_partial_flagged_result():
    pages = Pages(
        updated=[page(node(1), next_cursor="1"), RuntimeError("rate limited")],
    )

    result = asyncio.run(service(pages).get(12))

    assert result.coverage.truncated
    assert result.coverage.prs == 1


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

    assert result.coverage.prs_with_start == 2
    stages = {s.key: s for s in result.lead_time}
    assert stages["start_to_open"].values_s == [8 * 3600]  # PR 2, merged
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

    assert result.coverage.prs_with_start == 2
    assert result.coverage.starts_by_name == 0
    stages = {s.key: s for s in result.lead_time}
    assert stages["start_to_open"].n == 1  # PR 1 merged, its own start (1h)
    assert stages["start_to_open"].values_s == [3600]


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
    client, pages = client_and_pages

    body = client.get("/api/aggregate?weeks=4").json()

    assert body["weeks"] == 4
    assert body["window_end"] == "2026-10-08T12:00:00Z"
    assert body["throughput"]["opened"][-1] == 1
    assert body["throughput"]["merged"][-1] == 1
    assert [s["key"] for s in body["lead_time"]][0] == "start_to_open"
    assert body["mix"]["granularity"] == "day"
    assert body["mix"]["states"][0]["label"] == pr_states.PHASES["working"].label
    assert body["notes"]


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
