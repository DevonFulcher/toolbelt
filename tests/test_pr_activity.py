"""Ages, CI failure counting and the event timeline, from canned PR facts."""

from datetime import UTC, datetime, timedelta

from toolbelt.ui import pr_activity as pa
from toolbelt.ui.pr_activity import PrFacts, TimelineItem, derive_activity, format_age

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=5)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def item(hours: float, kind: str, **kwargs) -> TimelineItem:
    kwargs.setdefault("actor", "devon")
    return TimelineItem(time=at(hours), kind=kind, **kwargs)


def facts(**overrides) -> PrFacts:
    base = dict(
        number=5,
        title="Add the thing",
        author="devon",
        base_ref="main",
        head_oid="abc",
        state="OPEN",
        is_draft=False,
        created_at=at(0),
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=(),
    )
    base.update(overrides)
    return PrFacts(**base)  # type: ignore[arg-type]


class TestAges:
    def test_pr_age_runs_from_creation(self):
        assert derive_activity(facts(), NOW).pr_age == timedelta(days=5)

    def test_a_merged_pr_stops_ageing_when_merged(self):
        merged = facts(state="MERGED", merged_at=at(30))

        assert derive_activity(merged, NOW).pr_age == timedelta(hours=30)

    def test_a_closed_pr_stops_ageing_when_closed(self):
        closed = facts(state="CLOSED", closed_at=at(2))

        assert derive_activity(closed, NOW).pr_age == timedelta(hours=2)

    def test_draft_age_is_none_unless_draft(self):
        assert derive_activity(facts(), NOW).draft_age is None

    def test_a_pr_opened_as_draft_has_been_draft_since_creation(self):
        activity = derive_activity(facts(is_draft=True), NOW)

        assert activity.draft_age == timedelta(days=5)
        assert activity.draft_since == at(0)

    def test_draft_age_counts_from_the_latest_conversion_to_draft(self):
        history = (
            item(1, pa.READY_FOR_REVIEW),
            item(10, pa.CONVERTED_TO_DRAFT),
            item(20, pa.READY_FOR_REVIEW),
            item(40, pa.CONVERTED_TO_DRAFT),
        )

        activity = derive_activity(facts(is_draft=True, items=history), NOW)

        assert activity.draft_since == at(40)
        assert activity.draft_age == NOW - at(40)

    def test_a_merged_draft_state_is_not_a_draft_age(self):
        merged = facts(is_draft=True, state="MERGED", merged_at=at(3))

        assert derive_activity(merged, NOW).draft_age is None

    def test_review_request_age_is_the_oldest_pending_request(self):
        history = (
            item(5, pa.REVIEW_REQUESTED, subject="sam"),
            item(8, pa.REVIEW_REQUESTED, subject="lee"),
        )

        activity = derive_activity(
            facts(requested_reviewers=("sam", "lee"), items=history), NOW
        )

        assert activity.review_request_since == at(5)
        assert activity.review_request_age == NOW - at(5)

    def test_a_request_that_was_removed_or_answered_does_not_count(self):
        history = (
            item(5, pa.REVIEW_REQUESTED, subject="sam"),
            item(6, pa.REVIEW_REQUEST_REMOVED, subject="sam"),
            item(8, pa.REVIEW_REQUESTED, subject="lee"),
        )

        # Only lee is in the PR's current request list.
        activity = derive_activity(
            facts(requested_reviewers=("lee",), items=history), NOW
        )

        assert activity.review_request_since == at(8)

    def test_a_re_request_restarts_that_reviewers_clock(self):
        history = (
            item(5, pa.REVIEW_REQUESTED, subject="sam"),
            item(6, pa.REVIEW, subject="sam", detail="requested changes"),
            item(30, pa.REVIEW_REQUESTED, subject="sam"),
        )

        activity = derive_activity(
            facts(requested_reviewers=("sam",), items=history), NOW
        )

        assert activity.review_request_since == at(30)

    def test_a_request_made_before_going_ready_counts_from_ready(self):
        history = (
            item(1, pa.REVIEW_REQUESTED, subject="sam"),
            item(9, pa.READY_FOR_REVIEW),
        )

        activity = derive_activity(
            facts(requested_reviewers=("sam",), items=history), NOW
        )

        assert activity.review_request_since == at(9)

    def test_no_pending_review_has_no_age(self):
        assert derive_activity(facts(), NOW).review_request_age is None

    def test_a_pending_request_with_no_dated_event_has_no_age(self):
        # The timeline was truncated: the request itself is still listed.
        activity = derive_activity(facts(requested_reviewers=("sam",)), NOW)

        assert activity.review_request_age is None

    def test_a_merged_pr_has_no_pending_review(self):
        history = (item(5, pa.REVIEW_REQUESTED, subject="sam"),)
        merged = facts(
            state="MERGED",
            merged_at=at(9),
            requested_reviewers=("sam",),
            items=history,
        )

        assert derive_activity(merged, NOW).review_request_age is None


class TestCiFailureCount:
    def test_counts_failed_commits_not_jobs(self):
        # Three pushed commits: one failed (however many jobs), one errored,
        # one passed. Rollups are per commit, so jobs never enter into it.
        rollups = (("a1", "FAILURE"), ("b2", "ERROR"), ("c3", "SUCCESS"))

        assert derive_activity(facts(commit_rollups=rollups), NOW).ci_failure_count == 2

    def test_pending_and_missing_ci_are_not_failures(self):
        rollups = (("a1", "PENDING"), ("b2", None), ("c3", "EXPECTED"))

        assert derive_activity(facts(commit_rollups=rollups), NOW).ci_failure_count == 0

    def test_the_same_commit_is_counted_once(self):
        rollups = (("a1", "FAILURE"), ("a1", "FAILURE"))

        assert derive_activity(facts(commit_rollups=rollups), NOW).ci_failure_count == 1

    def test_force_pushed_away_commits_still_count(self):
        history = (
            item(3, pa.FORCE_PUSH, oid="dead", ci="FAILURE"),
            item(4, pa.FORCE_PUSH, oid="beef", ci="SUCCESS"),
        )
        rollups = (("a1", "FAILURE"),)

        activity = derive_activity(facts(commit_rollups=rollups, items=history), NOW)

        assert activity.ci_failure_count == 2

    def test_a_force_pushed_commit_that_is_also_listed_counts_once(self):
        history = (item(3, pa.FORCE_PUSH, oid="a1", ci="FAILURE"),)
        rollups = (("a1", "FAILURE"),)

        activity = derive_activity(facts(commit_rollups=rollups, items=history), NOW)

        assert activity.ci_failure_count == 1


class TestTimeline:
    def test_starts_with_opened_and_is_in_time_order(self):
        history = (
            item(9, pa.MERGED, detail="merged"),
            item(2, pa.COMMIT, detail="abc1234 Fix"),
            item(5, pa.REVIEW, actor="sam", detail="approved"),
        )

        timeline = derive_activity(facts(items=history), NOW).timeline

        assert [(e.kind, e.actor) for e in timeline] == [
            (pa.OPENED, "devon"),
            (pa.COMMIT, "devon"),
            (pa.REVIEW, "sam"),
            (pa.MERGED, "devon"),
        ]
        assert timeline[0].detail == "Add the thing"
        assert timeline[0].time == at(0)

    def test_events_at_the_same_instant_keep_github_order(self):
        history = (
            item(3, pa.READY_FOR_REVIEW),
            item(3, pa.REVIEW_REQUESTED, subject="sam"),
        )

        timeline = derive_activity(facts(items=history), NOW).timeline

        assert [e.kind for e in timeline] == [
            pa.OPENED,
            pa.READY_FOR_REVIEW,
            pa.REVIEW_REQUESTED,
        ]


class TestFormatAge:
    def test_compact_units(self):
        def fmt(**kwargs):
            return format_age(timedelta(**kwargs))

        assert fmt(seconds=30) == "<1m"
        assert fmt(minutes=5) == "5m"
        assert fmt(minutes=59) == "59m"
        assert fmt(hours=3) == "3h"
        assert fmt(hours=23) == "23h"
        assert fmt(days=1) == "1d"
        assert fmt(days=13) == "13d"
        assert fmt(days=14) == "2w"
        assert fmt(days=75) == "10w"
        assert fmt(seconds=-5) == "<1m"


class TestPreOpenCommits:
    def test_commits_pushed_before_the_pr_was_opened_come_before_opened(self):
        # The PR's commits are reported whatever their date; the client puts
        # the ones before the PR opened inside the task's working phase.
        activity = derive_activity(
            facts(
                items=(
                    item(-30, pa.COMMIT, detail="aaa1111 first"),
                    item(-2, pa.COMMIT, detail="bbb2222 second"),
                    item(1, pa.COMMIT, detail="ccc3333 third"),
                )
            ),
            NOW,
        )

        assert [(e.kind, e.detail) for e in activity.timeline] == [
            ("commit", "aaa1111 first"),
            ("commit", "bbb2222 second"),
            ("opened", "Add the thing"),
            ("commit", "ccc3333 third"),
        ]

    def test_the_pr_age_still_counts_from_the_pr_not_the_commits(self):
        activity = derive_activity(
            facts(items=(item(-30, pa.COMMIT, detail="aaa1111 first"),)), NOW
        )

        assert activity.pr_age == timedelta(days=5)


class TestCommitCiPhases:
    def test_a_failed_commit_runs_until_its_checks_complete_and_then_fails(self):
        assert pa.commit_ci_phases("failed", at(1), at(2)) == (
            pa.CiPhase("running", at(1), at(2)),
            pa.CiPhase("failing", at(2)),
        )

    def test_a_passing_commit_only_runs(self):
        assert pa.commit_ci_phases("success", at(1), at(2)) == (
            pa.CiPhase("running", at(1), at(2)),
        )

    def test_without_a_completion_time_pending_runs_and_failed_fails_from_push(self):
        assert pa.commit_ci_phases("pending", at(1), None) == (
            pa.CiPhase("running", at(1)),
        )
        assert pa.commit_ci_phases("failed", at(1), None) == (
            pa.CiPhase("failing", at(1)),
        )
        assert pa.commit_ci_phases("success", at(1), None) == ()
        assert pa.commit_ci_phases("", at(1), at(2)) == ()

    def test_checks_that_completed_before_the_push_do_not_run_after_it(self):
        assert pa.commit_ci_phases("failed", at(3), at(2)) == (
            pa.CiPhase("failing", at(3)),
        )
        assert pa.commit_ci_phases("success", at(3), at(2)) == ()

    def test_the_timeline_event_carries_the_completion_time(self):
        facts_ = facts(
            items=(
                item(1, pa.COMMIT, oid="c1", ci="FAILURE", ci_done=at(2)),
                item(3, pa.COMMIT, oid="c2", ci="SUCCESS"),
            )
        )

        events = [
            e for e in derive_activity(facts_, NOW).timeline if e.kind == pa.COMMIT
        ]

        assert [e.ci_done for e in events] == [at(2), None]
