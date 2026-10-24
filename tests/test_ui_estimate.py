"""The finish estimate in the UI: the stage history built from the aggregate's
replay, the stored backtest and its parameters, the live estimate of a card
and the track record. Pure inputs, a temp stack DB and injected clocks."""

import asyncio
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from test_ui_aggregate import at, merged, pr, request, review

from toolbelt.finish_estimate.model import MODEL_VERSION, Params
from toolbelt.finish_estimate.working_hours import WorkingHours
from toolbelt.git.stack.store import (
    BacktestRow,
    PredictionRow,
    StackStore,
    StageDurationRow,
)
from toolbelt.ui import aggregate as agg
from toolbelt.ui import pr_activity as act
from toolbelt.ui.aggregate_feed import parse_pr
from toolbelt.ui.estimate_feed import (
    EstimateJob,
    LiveTask,
    ModelHub,
    accuracy,
    live_estimates,
    samples_of,
    stage_rows,
    stored_params,
)

HOURS = WorkingHours(tz=UTC)
HOUR = 3600.0


def lives_of(records: list[agg.PrRecord], now: datetime):
    tasks = [agg.Task(pr=record, started=None) for record in records]
    with_pr, resolved, _ = agg.pr_lives(tasks, now)
    return list(zip(with_pr, resolved, strict=True))


def day_at(day: int, hour: float) -> float:
    """Hours after MONDAY of working day ``day`` (weekends skipped)."""
    return ((day // 5) * 7 + day % 5) * 24 + hour


def merged_pr(i: int, reviewer: str, wait_hours: float, repo: str = "o/api"):
    """A PR opened at 10:00 on working day ``i``, review requested at 10:30,
    approved ``wait_hours`` later and merged an hour after that."""
    start = day_at(i, 10)
    approved = start + 0.5 + wait_hours
    return pr(
        number=i + 1,
        created=start,
        merged_at=approved + 1,
        repo=repo,
        head=f"devon/pr-{i + 1}",
        items=(
            request(start + 0.5, reviewer),
            review(approved, "APPROVED", reviewer),
            merged(approved + 1),
        ),
    )


def history_records(count: int = 40) -> list[agg.PrRecord]:
    return [
        merged_pr(i, "ann" if i % 2 == 0 else "bob", 2 if i % 2 == 0 else 20)
        for i in range(count)
    ]


class TestStageRows:
    def test_a_merged_pr_has_a_row_per_stage_with_reviewer_and_finish(self):
        record = merged_pr(0, "ann", 3)
        now = at(day_at(1, 12))

        rows = stage_rows(lives_of([record], now), now)

        waiting = [row for row in rows if row.stage == "wait_review"]
        assert len(waiting) == 1
        (row,) = waiting
        assert row.task_key == "o/api#1"
        assert row.reviewer == "ann"
        assert row.kind == "others"
        assert row.actor == "ann"
        assert row.started_at == at(10.5)
        assert row.ended_at == at(13.5)
        assert row.finished_at == at(14.5)
        assert row.round == 1
        assert {r.finished_at for r in rows} == {at(14.5)}
        assert all(r.ended_at <= at(14.5) for r in rows)

    def test_the_stage_still_going_is_not_a_row(self):
        record = pr(number=3, created=10, items=(request(11, "ann"),))
        now = at(20)

        rows = stage_rows(lives_of([record], now), now)

        assert [r.stage for r in rows if r.ended_at >= now] == []
        assert "wait_review" not in {r.stage for r in rows}

    def test_a_closed_pr_has_stages_but_no_finish(self):
        record = pr(number=4, created=10, closed_at=30, items=(request(11, "ann"),))
        now = at(40)

        rows = stage_rows(lives_of([record], now), now)

        assert rows
        assert {r.finished_at for r in rows} == {None}

    def test_the_review_round_counts_the_stage_again(self):
        record = pr(
            number=5,
            created=10,
            merged_at=40,
            items=(
                request(11, "ann"),
                review(13, "CHANGES_REQUESTED", "ann"),
                request(20, "ann"),
                review(22, "APPROVED", "ann"),
                merged(23),
            ),
            review_now="approved",
        )
        now = at(50)

        rows = stage_rows(lives_of([record], now), now)

        assert [r.round for r in rows if r.stage == "wait_review"] == [1, 2]

    def test_size_is_log2_of_the_lines_changed(self):
        record = pr(
            number=6, created=10, merged_at=14, items=(request(11, "ann"), merged(14))
        )
        facts = record.facts.__class__(
            **{**record.facts.__dict__, "additions": 30, "deletions": 2}
        )
        record = agg.PrRecord(**{**record.__dict__, "facts": facts})
        now = at(20)

        rows = stage_rows(lives_of([record], now), now)

        assert {(r.additions, r.deletions, r.size_bucket) for r in rows} == {(30, 2, 5)}


class TestReviewers:
    def test_pending_reviewers_are_the_longest_waiting_first_and_a_review_settles_one(
        self,
    ):
        items = (
            act.TimelineItem(at(1), act.REVIEW_REQUESTED, "me", subject="bob"),
            act.TimelineItem(at(2), act.REVIEW_REQUESTED, "me", subject="ann"),
            act.TimelineItem(at(3), act.REVIEW, "bob", subject="bob"),
        )
        assert act.pending_reviewers(items) == ("ann",)
        assert act.pending_reviewers(items, at(2.5)) == ("bob", "ann")
        assert act.pending_reviewers(()) == ()

    def test_after_a_review_the_reviewer_is_who_reviewed(self):
        items = (
            act.TimelineItem(at(1), act.REVIEW_REQUESTED, "me", subject="bob"),
            act.TimelineItem(at(3), act.REVIEW, "bob", subject="bob"),
        )
        assert act.reviewer_of(items, at(2), "me") == "bob"
        assert act.reviewer_of(items, at(4), "me") == "bob"
        assert act.reviewer_of(items, at(0.5), "me") == ""

    def test_the_aggregate_parser_keeps_who_an_open_pr_waits_on(self):
        node = {
            "id": "PR_1",
            "number": 9,
            "title": "t",
            "state": "OPEN",
            "isDraft": False,
            "createdAt": "2026-10-05T00:00:00Z",
            "baseRefName": "main",
            "headRefName": "devon/x",
            "repository": {"nameWithOwner": "O/Api", "isArchived": False},
            "timelineItems": {
                "nodes": [
                    {
                        "__typename": "ReviewRequestedEvent",
                        "createdAt": "2026-10-05T01:00:00Z",
                        "actor": {"login": "me"},
                        "requestedReviewer": {"__typename": "User", "login": "ann"},
                    }
                ]
            },
        }
        assert parse_pr(node).facts.requested_reviewers == ("ann",)


class TestStore:
    def test_stage_rows_are_replaced_per_task(self, tmp_path: Path):
        store = StackStore(tmp_path / "s.db")
        try:

            def row(key: str, stage: str, start: int) -> StageDurationRow:
                return StageDurationRow(
                    task_key=key,
                    task_id=None,
                    stage=stage,
                    kind="others",
                    started_at=at(start),
                    ended_at=at(start + 1),
                    actor="ann",
                    reviewer="ann",
                    repo="o/api",
                    additions=1,
                    deletions=2,
                    size_bucket=1,
                    round=1,
                    finished_at=None,
                )

            store.replace_stage_durations(
                [row("a", "wait_ci", 1), row("b", "wait_ci", 1)]
            )
            store.replace_stage_durations([row("a", "merge", 5)])

            assert [(r.task_key, r.stage) for r in store.stage_durations()] == [
                ("b", "wait_ci"),
                ("a", "merge"),
            ]
        finally:
            store.close()

    def test_the_first_prediction_of_a_stage_is_kept(self, tmp_path: Path):
        store = StackStore(tmp_path / "s.db")
        try:
            first = PredictionRow(
                "a", "wait_review", at(1), at(2), at(10), at(20), "stage", 9, 1
            )
            assert store.record_prediction(first)
            later = PredictionRow(
                "a", "wait_review", at(1), at(5), at(99), at(99), "stage", 9, 1
            )
            assert not store.record_prediction(later)
            assert [p.p50_at for p in store.predictions()] == [at(10)]
        finally:
            store.close()

    def test_backtests_come_back_oldest_first(self, tmp_path: Path):
        store = StackStore(tmp_path / "s.db")
        try:
            for day in (3, 1, 2):
                store.save_backtest(BacktestRow(at(day * 24), 1, "{}", "{}"))
            assert [r.ran_at for r in store.backtests()] == [at(24), at(48), at(72)]
            assert [r.ran_at for r in store.backtests(limit=2)] == [at(48), at(72)]
        finally:
            store.close()


def cards(*args, **kwargs):
    return live_estimates(*args, **kwargs)[0]


def run_job(store_path: Path, records: list[agg.PrRecord], now: datetime, **kw) -> bool:
    async def lives():
        return lives_of(records, now)

    job = EstimateJob(
        db_path=lambda: store_path,
        lives=lives,
        hours=lambda: HOURS,
        wall_clock=lambda: now,
        challengers=(),
        ablation=None,
    )
    return asyncio.run(job.run_once(**kw))


class TestJob:
    def test_it_stores_the_history_and_a_backtest_whose_parameters_are_used(
        self, tmp_path: Path
    ):
        db = tmp_path / "s.db"
        now = at(day_at(60, 12))

        ran = run_job(db, history_records(60), now)

        assert ran
        store = StackStore(db)
        try:
            assert len(store.stage_durations()) >= 60
            (backtest,) = store.backtests()
            assert backtest.ran_at == now
            assert backtest.model_version == MODEL_VERSION
            assert stored_params(store) == Params(**json.loads(backtest.params))
            metrics = json.loads(backtest.metrics)
            assert metrics["overall"]["n"] > 0
            assert {s["stage"] for s in metrics["by_stage"]} >= {"wait_review"}
        finally:
            store.close()

    def test_a_backtest_runs_once_a_day_unless_forced(self, tmp_path: Path):
        db = tmp_path / "s.db"
        records = history_records(30)
        now = at(day_at(40, 12))

        assert run_job(db, records, now)
        assert not run_job(db, records, now + timedelta(hours=3))
        assert run_job(db, records, now + timedelta(hours=3), force_backtest=True)
        assert run_job(db, records, now + timedelta(days=1, hours=4))

        store = StackStore(db)
        try:
            assert len(store.backtests()) == 3
        finally:
            store.close()

    def test_rebuilding_the_history_does_not_duplicate_it(self, tmp_path: Path):
        db = tmp_path / "s.db"
        records = history_records(20)
        now = at(day_at(30, 12))
        run_job(db, records, now)
        store = StackStore(db)
        count = len(store.stage_durations())
        store.close()

        run_job(db, records, now + timedelta(hours=1))

        store = StackStore(db)
        try:
            assert len(store.stage_durations()) == count
        finally:
            store.close()


def live(task_id: str, record: agg.PrRecord, now: datetime) -> LiveTask:
    ((_, segments),) = lives_of([record], now)
    return LiveTask(
        task_id=task_id,
        repo=record.repo,
        branch=record.head_branch,
        facts=record.facts,
        segments=segments,
    )


class TestLiveEstimate:
    def seeded(self, tmp_path: Path, now: datetime) -> StackStore:
        db = tmp_path / "s.db"
        run_job(db, history_records(60), now)
        return StackStore(db)

    def open_pr(self, number: int, reviewer: str, asked: float, **kw):
        return pr(
            number=number,
            created=asked - 0.5,
            head=f"devon/open-{number}",
            items=(request(asked, reviewer),),
            review_now="requested",
            **kw,
        )

    def test_the_reviewer_changes_the_estimate_and_the_explanation_names_them(
        self, tmp_path: Path
    ):
        now = at(day_at(61, 11))
        store = self.seeded(tmp_path, now)
        try:
            fast = self.open_pr(900, "ann", day_at(61, 10))
            slow = self.open_pr(901, "bob", day_at(61, 10))
            found = cards(
                store, [live("fast", fast, now), live("slow", slow, now)], HOURS, now
            )
        finally:
            store.close()

        ann, bob = found["fast"], found["slow"]
        assert ann.known and bob.known
        assert ann.stage == bob.stage == "wait_review"
        assert ann.finish_p50 is not None and bob.finish_p50 is not None
        assert ann.finish_p50 < bob.finish_p50
        assert "waiting on ann" in ann.explanation
        assert "waiting on bob" in bob.explanation
        assert ann.level.startswith("reviewer")

    def test_the_estimate_is_unknown_without_history(self, tmp_path: Path):
        now = at(day_at(1, 11))
        store = StackStore(tmp_path / "s.db")
        try:
            found = cards(
                store,
                [live("t", self.open_pr(1, "ann", day_at(1, 10)), now)],
                HOURS,
                now,
            )
        finally:
            store.close()

        estimate = found["t"]
        assert not estimate.known
        assert (estimate.finish_p50, estimate.finish_p80) == (None, None)
        assert estimate.explanation.startswith("unknown: no history for this stage yet")

    def test_a_parent_landing_late_holds_back_its_child(self, tmp_path: Path):
        now = at(day_at(61, 11))
        store = self.seeded(tmp_path, now)
        try:
            parent = self.open_pr(910, "bob", day_at(61, 10))
            child = pr(
                number=911,
                created=day_at(61, 10),
                head="devon/child",
                base=parent.head_branch,
                items=(request(day_at(61, 10), "ann"),),
                review_now="requested",
            )
            alone = cards(store, [live("child", child, now)], HOURS, now)["child"]
            stacked = cards(
                store,
                [live("parent", parent, now), live("child", child, now)],
                HOURS,
                now,
            )
        finally:
            store.close()

        assert alone.finish_p50 is not None
        assert stacked["child"].finish_p50 is not None
        assert stacked["parent"].finish_p50 is not None
        assert stacked["child"].finish_p50 >= stacked["parent"].finish_p50
        assert stacked["child"].finish_p50 > alone.finish_p50

    def test_each_stage_entered_is_predicted_once(self, tmp_path: Path):
        now = at(day_at(61, 11))
        store = self.seeded(tmp_path, now)
        try:
            task = live("t", self.open_pr(920, "ann", day_at(61, 10)), now)
            cards(store, [task], HOURS, now)
            cards(store, [task], HOURS, now + timedelta(hours=1))
            (made,) = store.predictions()
        finally:
            store.close()

        assert made.task_key == "o/api#920"
        assert made.stage == "wait_review"
        assert made.made_at == now
        assert made.p50_at <= made.p80_at

    def test_a_merged_pr_has_no_estimate(self, tmp_path: Path):
        now = at(day_at(61, 11))
        store = self.seeded(tmp_path, now)
        try:
            done = merged_pr(930, "ann", 2)
            assert cards(store, [live("done", done, now)], HOURS, now) == {}
        finally:
            store.close()


class TestAccuracy:
    def test_it_reports_backtests_and_how_shown_estimates_fared(self, tmp_path: Path):
        db = tmp_path / "s.db"
        now = at(day_at(61, 11))
        run_job(db, history_records(60), now)
        store = StackStore(db)
        try:
            # An estimate shown for PR #3 (which finished) and one for an
            # unfinished PR; the first finished before its p80.
            store.record_prediction(
                PredictionRow(
                    "o/api#3",
                    "wait_review",
                    at(1),
                    at(2),
                    at(50),
                    at(60),
                    "stage",
                    9,
                    1,
                )
            )
            store.record_prediction(
                PredictionRow(
                    "o/api#999",
                    "wait_review",
                    at(1),
                    at(2),
                    at(50),
                    at(60),
                    "stage",
                    9,
                    1,
                )
            )
            track = accuracy(store, HOURS)
        finally:
            store.close()

        assert track.working_day_seconds == 8 * HOUR
        assert track.current is not None
        (latest,) = track.backtests
        assert latest.overall.stage == "all"
        assert latest.overall.coverage is not None
        assert 0 <= latest.overall.coverage <= 1
        assert {s.stage for s in latest.by_stage} >= {"wait_review"}
        assert (track.live_made, track.live_finished) == (2, 1)
        assert track.live_coverage in (0.0, 1.0)

    def test_no_backtest_yet_says_so(self, tmp_path: Path):
        store = StackStore(tmp_path / "s.db")
        try:
            track = accuracy(store, HOURS)
        finally:
            store.close()
        assert track.current is None
        assert track.backtests == []
        assert track.live_coverage is None


def test_samples_are_in_working_time(tmp_path: Path):
    start = day_at(4, 16)  # Friday 16:00
    row = StageDurationRow(
        task_key="a",
        task_id=None,
        stage="wait_review",
        kind="others",
        started_at=at(start),
        ended_at=at(start + 66),
        actor="x",
        reviewer="x",
        repo="r",
        additions=0,
        deletions=0,
        size_bucket=0,
        round=1,
        finished_at=at(start + 66 + 24),
    )
    (sample,) = samples_of([row], WorkingHours(tz=UTC, start=time(9), end=time(17)))
    # Friday 16:00 to Monday 10:00 is two working hours; Monday 10:00 to
    # Tuesday 10:00 is a working day.
    assert sample.seconds == 2 * HOUR
    assert sample.tail_seconds == 8 * HOUR


class TestConfig:
    def test_working_hours_come_from_the_config_file(self, tmp_path: Path):
        from toolbelt.finish_estimate.config import configured_working_hours

        config = tmp_path / "config.yaml"
        config.write_text(
            "estimate:\n  working_hours:\n    days: [mon, tue]\n"
            '    start: "08:00"\n    end: "12:00"\n    timezone: UTC\n'
        )

        hours = configured_working_hours(config)

        assert hours.days == (0, 1)
        assert hours.day_seconds == 4 * HOUR
        assert configured_working_hours(tmp_path / "missing.yaml").days == (
            0,
            1,
            2,
            3,
            4,
        )


class TestCommand:
    def test_backtest_now_rebuilds_the_history_from_the_stored_prs(
        self, tmp_path: Path
    ):
        from toolbelt.git.stack.store import AggregatePrRow, AggregateSync
        from toolbelt.ui.aggregate_feed import RECORD_SCHEMA, record_to_json
        from toolbelt.ui.estimate_feed import backtest_now

        db = tmp_path / "s.db"
        now = datetime.now(UTC)
        store = StackStore(db)
        try:
            store.save_aggregate(
                changed=[
                    AggregatePrRow(
                        repo=r.repo,
                        number=r.facts.number,
                        updated_at=None,
                        record=record_to_json(r),
                    )
                    for r in history_records(30)
                ],
                removed=[],
                sync=AggregateSync(
                    covered_from=now,
                    fetched_at=now,
                    refreshed=0,
                    truncated=False,
                    hidden=0,
                    open_swept_at=None,
                    schema=RECORD_SCHEMA,
                ),
            )
        finally:
            store.close()

        lines = asyncio.run(backtest_now(db, tmp_path / "no-config.yaml"))

        assert lines[0].endswith("stage rows stored.")
        assert any(
            line.startswith("baseline:") and "coverage" in line for line in lines
        )
        assert any(line.startswith("champion for all:") for line in lines)
        store = StackStore(db)
        try:
            assert len(store.backtests()) == 1
        finally:
            store.close()

    def test_with_no_stored_history_it_says_there_is_nothing_to_backtest(
        self, tmp_path: Path
    ):
        from toolbelt.ui.estimate_feed import backtest_now

        lines = asyncio.run(backtest_now(tmp_path / "empty.db", tmp_path / "c.yaml"))

        assert lines == ["0 stage rows stored; no finished work to backtest yet."]


def models_job(
    db: Path,
    records: list[agg.PrRecord],
    now: datetime,
    hub=None,
    ablation="regression",
    **kw,
):
    async def lives():
        return lives_of(records, now)

    job = EstimateJob(
        db_path=lambda: db,
        lives=lives,
        hours=lambda: HOURS,
        wall_clock=lambda: now,
        hub=hub,
        ablation=ablation,
        **kw,
    )
    return job, asyncio.run(job.run_once(force_backtest=True))


class TestModels:
    def test_the_job_backtests_every_model_picks_champions_and_stores_the_fits(
        self, tmp_path: Path
    ):
        db = tmp_path / "s.db"
        now = at(day_at(60, 12))

        _, ran = models_job(db, history_records(60), now)

        assert ran
        store = StackStore(db)
        try:
            (backtest,) = store.backtests()
            metrics = json.loads(backtest.metrics)
            assert set(metrics["models"]) == {
                "baseline",
                "regression",
                "hazard",
                "trees",
            }
            assert metrics["champions"]["all"]["model"] in metrics["models"]
            assert {
                e["model"] for e in metrics["champions"]["wait_review"]["evidence"]
            } == set(metrics["models"])
            assert metrics["ablation_model"] == "regression"
            assert {
                "size_log2",
                "queue_length",
                "slack_hours_since",
                "reviewer_open_requests",
            } <= {a["feature"] for a in metrics["ablation"]}
            assert {f.model for f in store.model_fits()} == {
                "regression",
                "hazard",
                "trees",
            }
            track = accuracy(store, HOURS)
        finally:
            store.close()
        assert {m.model for m in track.backtests[-1].models} == {
            "baseline",
            "regression",
            "hazard",
            "trees",
        }
        assert {f.model for f in track.fits} == {"regression", "hazard", "trees"}

    def test_a_restart_restores_the_fits_instead_of_refitting(self, tmp_path: Path):
        db = tmp_path / "s.db"
        now = at(day_at(60, 12))
        models_job(db, history_records(40), now, ablation=None)

        hub = ModelHub()
        store = StackStore(db)
        try:
            assert hub.due(store, now + timedelta(hours=2)) == []
            assert set(hub.models) == {"regression", "hazard", "trees"}
            assert hub.due(store, now + timedelta(days=1, hours=1)) == [
                "regression",
                "hazard",
                "trees",
            ]
        finally:
            store.close()

    def test_new_finished_stages_make_a_refit_due(self, tmp_path: Path):
        db = tmp_path / "s.db"
        now = at(day_at(60, 12))
        models_job(db, history_records(40), now, ablation=None)
        hub = ModelHub()
        store = StackStore(db)
        try:
            assert hub.due(store, now) == []
            store.replace_stage_durations(
                [
                    StageDurationRow(
                        task_key=f"o/api#{900 + i}",
                        task_id=None,
                        stage="wait_ci",
                        kind="ci",
                        started_at=at(i),
                        ended_at=at(i + 1),
                        actor="ci",
                        reviewer="",
                        repo="o/api",
                        additions=1,
                        deletions=1,
                        size_bucket=1,
                        round=1,
                        finished_at=None,
                    )
                    for i in range(30)
                ]
            )
            assert hub.due(store, now) == ["regression", "hazard", "trees"]
        finally:
            store.close()

    def test_the_card_shows_the_champions_range_and_the_popup_every_model(
        self, tmp_path: Path
    ):
        db = tmp_path / "s.db"
        now = at(day_at(61, 11))
        hub = ModelHub()
        models_job(db, history_records(50), at(day_at(60, 12)), hub=hub, ablation=None)
        store = StackStore(db)
        try:
            task = live(
                "t", TestLiveEstimate.open_pr(None, 920, "bob", day_at(61, 10)), now
            )
            found, details = live_estimates(store, [task], HOURS, now, hub)
        finally:
            store.close()

        card, popup = found["t"], details["t"]
        assert card.model == popup.champion
        assert [m.model for m in popup.models] == [
            "baseline",
            "regression",
            "hazard",
            "trees",
        ]
        assert sum(m.champion for m in popup.models) == 1
        champion = next(m for m in popup.models if m.champion)
        assert champion.finish_p50 == card.finish_p50
        assert {a.model for a in popup.accuracy} == {
            "baseline",
            "regression",
            "hazard",
            "trees",
        }
        assert (
            next(a for a in popup.accuracy if a.model == "regression").trained_on
            is not None
        )
        assert popup.champion_reason
        baseline = next(m for m in popup.models if m.model == "baseline")
        assert baseline.details["factors"] and baseline.details["similar"]
        assert popup.similar and popup.factors

    def test_the_champion_is_what_the_backtest_chose_for_the_stage(self):
        from toolbelt.ui.estimate_feed import champion_for

        metrics = {
            "champions": {
                "all": {"model": "baseline", "margin": 0.0, "points": 80},
                "wait_review": {"model": "trees", "margin": 0.12, "points": 41},
                "merge": {"model": "baseline", "margin": 0.0, "points": 12},
            }
        }
        model, reason = champion_for(metrics, "wait_review")
        assert model == "trees" and "12%" in reason and "41 test points" in reason
        assert champion_for(metrics, "merge")[0] == "baseline"
        assert "stays" in champion_for(metrics, "merge")[1]
        assert champion_for(metrics, "fix_ci")[0] == "baseline"
        assert champion_for({}, "fix_ci")[0] == "baseline"


class TestStackedModels:
    """Ancestors are never summed: a child's own work runs in parallel with
    its parent's, only the merge is ordered."""

    def seeded(self, tmp_path: Path):
        db = tmp_path / "s.db"
        now = at(day_at(61, 11))
        run_job(db, history_records(60), at(day_at(60, 12)))
        return StackStore(db), now

    def pr_with(self, number: int, reviewer: str, **kw):
        opened = day_at(61, 10)
        return pr(
            number=number,
            created=opened,
            head=kw.pop("head", f"devon/p{number}"),
            items=(request(opened + 0.25, reviewer),),
            review_now="requested",
            **kw,
        )

    def test_the_child_is_the_later_of_its_own_and_its_parents_finish(
        self, tmp_path: Path
    ):
        store, now = self.seeded(tmp_path)
        try:
            slow_parent = self.pr_with(1, "bob")
            fast_child = self.pr_with(2, "ann", base=slow_parent.head_branch)
            alone = cards(store, [live("c", fast_child, now)], HOURS, now)["c"]
            both = cards(
                store,
                [live("p", slow_parent, now), live("c", fast_child, now)],
                HOURS,
                now,
            )
        finally:
            store.close()

        assert both["c"].finish_p50 >= both["p"].finish_p50
        assert both["c"].finish_p50 > alone.finish_p50
        assert both["c"].finish_p80 >= both["c"].finish_p50

    def test_a_chain_recurses_through_the_nearest_parent_only(self, tmp_path: Path):
        store, now = self.seeded(tmp_path)
        try:
            grand = self.pr_with(1, "bob")
            parent = self.pr_with(2, "ann", base=grand.head_branch)
            child = self.pr_with(3, "ann", base=parent.head_branch)
            found = cards(
                store,
                [live("g", grand, now), live("p", parent, now), live("c", child, now)],
                HOURS,
                now,
            )
        finally:
            store.close()

        # The grandparent's wait reaches the child through its parent.
        assert found["g"].finish_p50 <= found["p"].finish_p50 <= found["c"].finish_p50

    def test_a_merged_parent_adds_no_wait(self, tmp_path: Path):
        store, now = self.seeded(tmp_path)
        try:
            merged_parent = merged_pr(950, "ann", 1)
            child = self.pr_with(4, "ann", base=merged_parent.head_branch)
            with_merged = cards(
                store,
                [live("m", merged_parent, now), live("c", child, now)],
                HOURS,
                now,
            )
            alone = cards(store, [live("c", child, now)], HOURS, now)
        finally:
            store.close()

        assert with_merged["c"].finish_p50 == alone["c"].finish_p50

    def test_a_parent_with_no_estimate_makes_the_child_at_least_its_own(
        self, tmp_path: Path
    ):
        store, now = self.seeded(tmp_path)
        try:
            # The parent is in a stage nothing was ever seen in: unknown.
            parent = pr(
                number=5,
                created=day_at(61, 10),
                head="devon/p5",
                draft=True,
                items=(),
                review_now="none",
            )
            child = self.pr_with(6, "ann", base=parent.head_branch)
            found, details = live_estimates(
                store, [live("p", parent, now), live("c", child, now)], HOURS, now
            )
            alone = cards(store, [live("c", child, now)], HOURS, now)["c"]
        finally:
            store.close()

        assert not found["p"].known
        assert found["c"].known
        assert found["c"].finish_p50 == alone.finish_p50
        assert "at least this: its parent has no estimate" in found["c"].explanation
        assert details["c"].parent is not None and not details["c"].parent.parent_known

    def test_siblings_never_affect_each_other(self, tmp_path: Path):
        store, now = self.seeded(tmp_path)
        try:
            parent = self.pr_with(7, "ann")
            a = self.pr_with(8, "ann", base=parent.head_branch, head="devon/a8")
            b = self.pr_with(9, "bob", base=parent.head_branch, head="devon/b9")
            together = cards(
                store,
                [live("p", parent, now), live("a", a, now), live("b", b, now)],
                HOURS,
                now,
            )
            only_a = cards(
                store, [live("p", parent, now), live("a", a, now)], HOURS, now
            )
        finally:
            store.close()

        assert together["a"].finish_p50 == only_a["a"].finish_p50

    def test_the_popup_shows_the_parent_wait_as_its_own_row(self, tmp_path: Path):
        store, now = self.seeded(tmp_path)
        try:
            parent = self.pr_with(10, "bob")
            child = self.pr_with(11, "ann", base=parent.head_branch)
            _, details = live_estimates(
                store, [live("p", parent, now), live("c", child, now)], HOURS, now
            )
        finally:
            store.close()

        wait = details["c"].parent
        assert wait is not None and wait.parent_known
        assert wait.task_key == "o/api#10"
        assert wait.combined_p50 >= wait.parent_p50
        assert details["p"].parent is None
