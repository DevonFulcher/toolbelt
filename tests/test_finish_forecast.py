"""The model-agnostic parts of the finish estimate (the feature registry, the
snapshot dataset, the stacking arithmetic, the backtest harness, champions and
ablation) and the challenger models, on synthetic worlds with a known
structure."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from toolbelt.finish_estimate.backtest import (
    Outcome,
    ablate,
    choose_champions,
    metrics_of,
    replay,
)
from toolbelt.finish_estimate.baseline import BaselineModel
from toolbelt.finish_estimate.challengers import (
    BoostedTreesModel,
    HazardModel,
    QuantileRegressionModel,
)
from toolbelt.finish_estimate.features import BY_NAME, REGISTRY, STAGE_DIMS, stage_kinds
from toolbelt.finish_estimate.forecast import (
    Forecast,
    TrainingData,
    full_grid,
    grid_add,
    grid_max,
    grid_zero,
    read_p50_p80,
)
from toolbelt.finish_estimate.model import LEVELS
from toolbelt.finish_estimate.snapshots import (
    Span,
    TaskTimeline,
    build_snapshots,
    samples_of_timelines,
    with_known_at,
)
from toolbelt.finish_estimate.working_hours import WorkingHours

HOURS = WorkingHours(tz=UTC)
HOUR = 3600.0
DAY = 8 * HOUR
START = datetime(2026, 1, 5, 9, tzinfo=UTC)  # a Monday, 09:00


def at(working_hours: float) -> datetime:
    return HOURS.advance(START, working_hours * HOUR)


def timeline(
    key: str,
    *,
    reviewer: str = "ann",
    start: float = 0.0,
    wait: float = 2.0,
    size: int = 3,
    outcome: str = "merged",
    slack: tuple[datetime, ...] = (),
    now: datetime | None = None,
) -> TaskTimeline:
    """open_pr (1h, you), wait_review (``wait`` working hours, others),
    merge (1h, you). For an ``open`` task the data ends at ``now`` inside the
    review wait."""
    t0 = at(start)
    t1 = HOURS.advance(t0, HOUR)
    t2 = HOURS.advance(t1, wait * HOUR)
    t3 = HOURS.advance(t2, HOUR)
    spans = [
        Span("open_pr", t0, t1, who="you"),
        Span(
            "wait_review", t1, None if outcome == "open" else t2, reviewer, who="others"
        ),
    ]
    if outcome != "open":
        spans.append(Span("merge", t2, t3, who="you"))
    return TaskTimeline(
        task_key=key,
        repo="o/r",
        size_bucket=size,
        spans=tuple(spans),
        outcome=outcome,  # type: ignore[arg-type]
        ended_at=t3 if outcome != "open" else None,
        slack_posts=slack,
    )


class TestRegistry:
    def test_every_kind_a_stage_can_be_has_an_explicit_entry(self):
        missing = set(stage_kinds()) - set(STAGE_DIMS)
        assert not missing, (
            f"add {sorted(missing)} to STAGE_DIMS (DEFAULT_DIMS is allowed)"
        )
        assert set(STAGE_DIMS) <= set(stage_kinds())

    def test_baseline_dimensions_are_stored_categorical_features(self):
        for stage, dims in STAGE_DIMS.items():
            for dim in dims:
                assert BY_NAME[dim].encoding == "categorical", (stage, dim)
                assert BY_NAME[dim].sample_attr, (stage, dim)

    def test_names_are_unique_and_the_candidate_features_are_declared(self):
        names = [f.name for f in REGISTRY]
        assert len(names) == len(set(names))
        assert {"reviewer_open_requests", "queue_length", "slack_hours_since"} <= set(
            names
        )


class TestSnapshots:
    def test_a_row_per_checkpoint_each_working_day_in_a_stage(self):
        long = timeline("a", wait=20)  # 2.5 working days in review
        rows = [
            r
            for r in build_snapshots([long], HOURS, at(60))
            if r.features.stage == "wait_review"
        ]

        assert [r.features.elapsed_seconds for r in rows] == [0, DAY, 2 * DAY]
        assert rows[0].stage_remaining_seconds == 20 * HOUR
        assert rows[1].stage_remaining_seconds == 12 * HOUR
        assert rows[0].finish_remaining_seconds == 21 * HOUR
        assert not rows[0].finish_censored

    def test_features_use_only_what_ended_strictly_before_the_checkpoint(self):
        early = timeline("early", reviewer="bob", start=0, wait=3)  # review ends at 4h
        later = timeline("later", reviewer="bob", start=10, wait=2)
        exactly = timeline(
            "exact", reviewer="bob", start=3, wait=1
        )  # starts 4h = early's end
        rows = {
            r.features.task_key: r
            for r in build_snapshots([early, later, exactly], HOURS, at(100))
            if r.features.stage == "wait_review" and r.features.elapsed_seconds == 0
        }

        assert rows["early"].features.value("reviewer_latency") is None
        # "exact" checks at 4h, the instant early's review ended: not before it.
        assert rows["exact"].features.value("reviewer_latency") is None
        # Both earlier reviews (3h and 1h) had ended: their median.
        assert rows["later"].features.value("reviewer_latency") == pytest.approx(
            2 * HOUR
        )

    def test_open_requests_of_the_reviewer_on_other_tasks_and_my_queue(self):
        a = timeline("a", reviewer="bob", start=0, wait=10)
        b = timeline("b", reviewer="bob", start=1, wait=10)
        rows = {
            r.features.task_key: r
            for r in build_snapshots([a, b], HOURS, at(100))
            if r.features.stage == "wait_review" and r.features.elapsed_seconds == 0
        }

        assert rows["a"].features.value("reviewer_open_requests") == 0
        assert rows["b"].features.value("reviewer_open_requests") == 1
        # b starts its review at 2h; a's open_pr (waiting on me) ended at 1h.
        assert rows["b"].features.value("queue_length") == 0

    def test_hours_since_the_last_slack_post_and_hour_of_week(self):
        task = timeline("a", wait=30, slack=(at(2), at(30)))
        rows = [
            r
            for r in build_snapshots([task], HOURS, at(60))
            if r.features.stage == "wait_review"
        ]

        assert (
            rows[0].features.value("slack_hours_since")
            == pytest.approx((rows[0].checkpoint - at(2)).total_seconds() / 3600)
            or rows[0].features.value("slack_hours_since") is None
        )
        assert rows[0].features.value("hour_of_week") == 9 + 1  # Monday 10:00

    def test_an_open_task_is_kept_and_flagged_censored(self):
        now = at(30)
        open_task = timeline("open", outcome="open", wait=0, now=now)
        merged = timeline("done", start=0, wait=3)
        rows = build_snapshots([open_task, merged], HOURS, now)

        mine = [r for r in rows if r.features.task_key == "open"]
        assert mine, "open tasks must not be dropped"
        assert all(
            r.finish_censored and r.stage_censored
            for r in mine
            if r.features.stage == "wait_review"
        )
        # The target is the time observed so far: a lower bound.
        first = next(r for r in mine if r.features.stage == "wait_review")
        assert first.finish_remaining_seconds == HOURS.seconds(first.checkpoint, now)
        assert not [
            r for r in rows if r.features.task_key == "done" and r.finish_censored
        ]

    def test_a_closed_task_has_a_stage_target_but_no_finish(self):
        closed = timeline("x", outcome="closed")
        rows = build_snapshots([closed], HOURS, at(60))
        assert rows
        assert all(r.finish_remaining_seconds is None for r in rows)
        assert all(not r.stage_censored for r in rows)

    def test_targets_are_censored_at_the_instant_they_are_known_at(self):
        task = timeline("a", wait=20)
        row = next(
            r
            for r in build_snapshots([task], HOURS, at(60))
            if r.features.stage == "wait_review"
        )
        known = with_known_at(row, at(10), HOURS)

        assert row.finish_censored is False
        assert known.finish_censored and known.stage_censored
        assert known.finish_remaining_seconds == HOURS.seconds(row.checkpoint, at(10))
        assert known.outcome == "open"

    def test_samples_carry_the_finish_for_the_tails(self):
        (first, *_) = samples_of_timelines([timeline("a", wait=4)], HOURS)
        assert first.stage == "open_pr"
        assert first.tail_seconds == 5 * HOUR  # 4h review + 1h merge


class TestStacking:
    A = tuple(float(i) for i in range(1, 20))  # 1..19

    def test_max_with_nothing_to_wait_for_is_the_other(self):
        assert grid_max(self.A, grid_zero()) == self.A
        assert grid_max(grid_zero(), self.A) == self.A

    def test_max_of_independent_distributions_is_the_product_of_their_cdfs(self):
        result = grid_max(self.A, self.A)
        # P(max <= x) = (x/19)^2 on this grid: the median is where it passes .5.
        assert result[9] == 14.0  # (14/19)^2 = .54 >= .5, (13/19)^2 = .47
        assert all(r >= a for r, a in zip(result, self.A, strict=True))
        # Never earlier than the larger of the medians (the point-estimate max).
        assert read_p50_p80(result)[0] >= max(self.A[9], self.A[9])

    def test_adding_a_tail_convolves_the_grids(self):
        tail = tuple([2.0] * 19)
        assert grid_add(self.A, tail) == tuple(a + 2.0 for a in self.A)
        wide = grid_add(self.A, self.A)
        assert wide[9] == pytest.approx(20.0, abs=1.0)  # median of a sum
        assert wide[0] >= 2 and wide[-1] <= 38

    def test_a_forecast_without_a_grid_gets_the_lognormal_through_its_quantiles(self):
        grid = full_grid(Forecast("m", 4 * HOUR, 8 * HOUR))
        assert grid is not None
        assert grid[LEVELS.index(0.5)] == pytest.approx(4 * HOUR)
        assert grid[LEVELS.index(0.8)] == pytest.approx(8 * HOUR)
        assert list(grid) == sorted(grid)
        assert full_grid(Forecast("m", None, None)) is None


def world(
    n: int, wait, *, seed: int = 1, open_tail: int = 0
) -> tuple[list[TaskTimeline], datetime]:
    """Tasks started half a working day apart; ``wait(rng, reviewer, size)``
    gives the review wait in working hours."""
    rng = random.Random(seed)
    tasks = []
    for i in range(n):
        reviewer = ("ann", "bob")[i % 2]
        size = rng.choice([1, 2, 3, 4, 5, 6])
        tasks.append(
            timeline(
                f"t{i}",
                reviewer=reviewer,
                start=i * 4.0,
                wait=wait(rng, reviewer, size),
                size=size,
            )
        )
    now = tasks[-1].ended_at + timedelta(days=1)  # type: ignore[operator]
    return tasks, now


def size_effect(rng, reviewer, size):
    return 0.4 * 2**size * rng.uniform(0.9, 1.1)


def reviewer_effect(rng, reviewer, size):
    return (2.0 if reviewer == "ann" else 16.0) * rng.uniform(0.85, 1.15)


def models(hours=HOURS):
    return {
        "baseline": BaselineModel(hours),
        "regression": QuantileRegressionModel(),
        "hazard": HazardModel(),
        "trees": BoostedTreesModel(),
    }


WEEK = timedelta(days=7)


class TestHarness:
    def test_every_model_is_scored_on_the_same_rows(self):
        tasks, now = world(70, reviewer_effect)
        out = replay(models(), tasks, HOURS, now, max_tasks=40, refit_every=WEEK)

        keys = {
            name: [(o.task_key, o.stage, o.made_at) for o in rows]
            for name, rows in out.items()
        }
        assert len({tuple(v) for v in keys.values()}) == 1
        assert len(out["baseline"]) > 60

    def test_the_baseline_covers_near_its_target_on_a_stable_world(self):
        tasks, now = world(120, lambda rng, r, s: rng.lognormvariate(1.5, 0.8))
        out = replay(
            {"baseline": BaselineModel(HOURS)},
            tasks,
            HOURS,
            now,
            max_tasks=80,
            refit_every=WEEK,
        )

        m = metrics_of(out["baseline"])
        assert m.coverage is not None and 0.65 <= m.coverage <= 0.95
        assert m.loss is not None and m.median_abs_error_seconds is not None

    def test_a_prediction_never_uses_its_own_future(self):
        tasks, now = world(30, reviewer_effect)
        out = replay(
            {"baseline": BaselineModel(HOURS)},
            tasks,
            HOURS,
            now,
            max_tasks=30,
            refit_every=WEEK,
        )
        first = min(out["baseline"], key=lambda o: o.made_at)
        assert first.p50 is None  # nothing had ended before the very first checkpoint

    def test_open_tasks_are_counted_and_a_miss_beyond_p80_is_reported(self):
        tasks, now = world(60, reviewer_effect)
        stuck = timeline(
            "stuck", reviewer="bob", start=0, outcome="open", wait=0, now=now
        )
        out = replay(
            {"baseline": BaselineModel(HOURS)},
            [*tasks, stuck],
            HOURS,
            now,
            max_tasks=100,
            refit_every=WEEK,
        )

        m = metrics_of(out["baseline"])
        assert m.censored > 0
        assert m.coverage_adjusted is not None and m.coverage is not None
        assert m.coverage_adjusted <= m.coverage


def outcome(stage, actual, p50, p80, task="t"):
    return Outcome(task, stage, START, actual, False, p50, p80, "x", 5)


class TestChampions:
    def rows(self, n, base_err, other_err, stage="wait_review"):
        base = [
            outcome(stage, 100.0, 100.0 + base_err, 150.0 + base_err) for _ in range(n)
        ]
        other = [
            outcome(stage, 100.0, 100.0 + other_err, 150.0 + other_err)
            for _ in range(n)
        ]
        return {"baseline": base, "regression": other}

    def test_the_best_model_is_promoted_when_it_beats_the_baseline_by_the_margin(self):
        champions = choose_champions(self.rows(40, 50.0, 5.0))
        assert champions["wait_review"].model == "regression"
        assert champions["wait_review"].margin > 0.05
        assert {e.model for e in champions["wait_review"].evidence} == {
            "baseline",
            "regression",
        }

    def test_no_promotion_without_enough_test_points(self):
        champions = choose_champions(self.rows(29, 50.0, 5.0))
        assert champions["wait_review"].model == "baseline"
        assert champions["wait_review"].points == 29

    def test_no_promotion_inside_the_margin(self):
        champions = choose_champions(self.rows(40, 50.0, 49.0))
        assert champions["wait_review"].model == "baseline"

    def test_each_stage_is_decided_on_its_own_points(self):
        rows = self.rows(40, 50.0, 5.0, "wait_review")
        merge = self.rows(40, 5.0, 50.0, "merge")
        out = {k: rows[k] + merge[k] for k in rows}
        champions = choose_champions(out)
        assert champions["wait_review"].model == "regression"
        assert champions["merge"].model == "baseline"


class TestChallengers:
    def test_the_regression_learns_a_size_effect_the_baseline_cannot_see(self):
        tasks, now = world(120, size_effect)
        out = replay(
            {"baseline": BaselineModel(HOURS), "regression": QuantileRegressionModel()},
            tasks,
            HOURS,
            now,
            max_tasks=60,
            refit_every=WEEK,
        )
        stage = {
            name: metrics_of([o for o in rows if o.stage == "wait_review"])
            for name, rows in out.items()
        }

        assert (
            stage["regression"].loss is not None and stage["baseline"].loss is not None
        )
        assert stage["regression"].loss < 0.8 * stage["baseline"].loss
        champion = choose_champions(out)["wait_review"]
        assert champion.model == "regression"

    def test_the_regression_explains_the_size_contribution(self):
        tasks, now = world(100, size_effect)
        samples = samples_of_timelines(tasks, HOURS)
        rows = build_snapshots(tasks, HOURS, now)
        model = QuantileRegressionModel()
        model.fit(TrainingData(now, HOURS, samples, rows))
        state = next(
            r.features
            for r in rows
            if r.features.stage == "wait_review" and r.features.value("size") == "6"
        )

        forecast = model.predict(state)

        assert forecast.known
        names = [c["feature"] for c in forecast.details["contributions"]]
        assert "size_log2" in names
        assert forecast.p50_seconds > 10 * HOUR  # 0.4 * 2**6 = 25.6 working hours

    def test_p80_never_crosses_below_p50(self):
        tasks, now = world(100, size_effect, seed=3)
        samples = samples_of_timelines(tasks, HOURS)
        rows = build_snapshots(tasks, HOURS, now)
        data = TrainingData(now, HOURS, samples, rows)
        for model in (QuantileRegressionModel(), HazardModel(), BoostedTreesModel()):
            model.fit(data)
            for row in rows[::7]:
                f = model.predict(row.features)
                if f.known:
                    assert f.p80_seconds >= f.p50_seconds, model.name

    def test_a_fitted_model_survives_a_restart(self):
        tasks, now = world(90, size_effect)
        data = TrainingData(
            now,
            HOURS,
            samples_of_timelines(tasks, HOURS),
            build_snapshots(tasks, HOURS, now),
        )
        for fresh, restored in (
            (QuantileRegressionModel(), QuantileRegressionModel()),
            (HazardModel(), HazardModel()),
            (BoostedTreesModel(), BoostedTreesModel()),
        ):
            fresh.fit(data)
            blob = fresh.dump()
            assert blob is not None
            restored.load(blob)
            state = data.rows[len(data.rows) // 2].features
            assert (
                restored.predict(state).p50_seconds == fresh.predict(state).p50_seconds
            )

    def test_too_little_data_is_unknown_and_so_is_an_unseen_stage(self):
        tasks, now = world(5, size_effect)
        data = TrainingData(
            now,
            HOURS,
            samples_of_timelines(tasks, HOURS),
            build_snapshots(tasks, HOURS, now),
        )
        model = QuantileRegressionModel()
        model.fit(data)
        assert not model.predict(data.rows[0].features).known

        many, now = world(100, size_effect)
        data = TrainingData(
            now,
            HOURS,
            samples_of_timelines(many, HOURS),
            build_snapshots(many, HOURS, now),
        )
        model.fit(data)
        state = data.rows[0].features
        new_stage = type(state)(**{**state.__dict__, "stage": "personal_review"})
        forecast = model.predict(new_stage)
        assert not forecast.known and "no history for this stage" in forecast.text

    def test_censored_open_tasks_pull_the_estimate_up(self):
        # Most finished tasks are quick; the slow ones are still open. Leaving
        # the open ones out would say everything is quick.
        quick, now = world(80, lambda rng, r, s: rng.uniform(1, 2))
        slow = [
            timeline(f"s{i}", reviewer="bob", start=i * 4.0 + 1, outcome="open", wait=0)
            for i in range(30)
        ]
        now = max(now, at(30 * 4 + 400))
        rows = build_snapshots([*quick, *slow], HOURS, now)
        samples = samples_of_timelines([*quick, *slow], HOURS)
        with_open = QuantileRegressionModel()
        with_open.fit(TrainingData(now, HOURS, samples, rows))
        without = QuantileRegressionModel()
        without.fit(
            TrainingData(
                now, HOURS, samples, [r for r in rows if not r.finish_censored]
            )
        )
        state = next(
            r.features
            for r in rows
            if r.features.stage == "wait_review" and r.features.task_key == "t10"
        )

        assert with_open.predict(state).p80_seconds > without.predict(state).p80_seconds

    def test_the_hazard_model_expresses_a_fast_and_a_slow_group(self):
        def mixture(rng, reviewer, size):
            return rng.uniform(0.5, 1.5) if rng.random() < 0.5 else rng.uniform(30, 40)

        tasks, now = world(160, mixture, seed=5)
        data = TrainingData(
            now,
            HOURS,
            samples_of_timelines(tasks, HOURS),
            build_snapshots(tasks, HOURS, now),
        )
        model = HazardModel()
        model.fit(data)
        state = next(
            r.features
            for r in data.rows
            if r.features.stage == "wait_review" and r.features.elapsed_seconds == 0
        )

        forecast = model.predict(state)

        assert forecast.known and forecast.grid is not None
        assert forecast.p50_seconds < 40 * HOUR
        assert forecast.p80_seconds > 25 * HOUR  # the slow group is in the upper tail
        hazard = forecast.details["hazard"]
        early = max(h["hazard"] for h in hazard if h["from_hours"] <= 2)
        middle = min(h["hazard"] for h in hazard if 4 <= h["from_hours"] <= 16)
        assert early > 3 * middle  # a fast group, then a lull

    def test_the_trees_report_which_features_mattered(self):
        tasks, now = world(140, size_effect)
        data = TrainingData(
            now,
            HOURS,
            samples_of_timelines(tasks, HOURS),
            build_snapshots(tasks, HOURS, now),
        )
        model = BoostedTreesModel()
        model.fit(data)
        state = next(r.features for r in data.rows if r.features.stage == "wait_review")

        forecast = model.predict(state)

        assert forecast.known
        assert forecast.details["importances"][0]["feature"] in {
            "size_log2",
            "stage",
            "elapsed",
        }


class TestAblation:
    def test_a_real_feature_costs_loss_when_removed_and_noise_does_not(self):
        tasks, now = world(120, size_effect, seed=2)
        rows = ablate(
            lambda excluded: QuantileRegressionModel(excluded),
            ["size_log2", "hour_of_week"],
            tasks,
            HOURS,
            now,
            max_tasks=60,
            refit_every=WEEK,
        )
        by = {r.feature: r for r in rows}

        assert by["size_log2"].delta is not None and by["size_log2"].delta > 0
        assert by["size_log2"].points > 30
        assert by["hour_of_week"].delta is not None
        assert by["hour_of_week"].delta < by["size_log2"].delta / 3


def test_trees_fit_when_a_column_has_values_only_on_rows_the_second_pass_drops():
    from dataclasses import replace

    quick, now = world(80, lambda rng, r, s: rng.uniform(1, 2))
    slow = [
        timeline(f"s{i}", reviewer="bob", start=i * 4.0 + 1, outcome="open", wait=0)
        for i in range(30)
    ]
    now = max(now, at(30 * 4 + 400))
    rows = build_snapshots([*quick, *slow], HOURS, now)

    def only_on_open(row):
        values = dict(row.features.values)
        values["reviewer_latency"] = (
            values["reviewer_latency"] if row.finish_censored else None
        )
        return replace(row, features=replace(row.features, values=values))

    data = TrainingData(
        now,
        HOURS,
        samples_of_timelines([*quick, *slow], HOURS),
        [only_on_open(r) for r in rows if "reviewer_latency" in r.features.values],
    )
    model = BoostedTreesModel()
    model.fit(data)
    state = next(r.features for r in data.rows if r.features.stage == "wait_review")
    assert model.predict(state).known
