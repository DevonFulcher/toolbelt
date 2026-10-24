"""The finish estimate on synthetic histories: exact quantiles, the fallback
level, working time, stacking, regime change and backtest coverage."""

import random
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from toolbelt.finish_estimate.backtest import (
    loss,
    metrics_of,
    regime_changes,
    replay,
    run_backtest,
)
from toolbelt.finish_estimate.model import (
    History,
    Params,
    StageSample,
    Subject,
    estimate,
    post_parent_tail,
    size_bucket,
    weighted_quantile,
)
from toolbelt.finish_estimate.working_hours import (
    WorkingHours,
    working_hours_from_config,
)

UTC_HOURS = WorkingHours(tz=UTC)
HOUR = 3600.0
DAY = 8 * HOUR
FRIDAY = datetime(2026, 10, 23, tzinfo=UTC)
START = datetime(2026, 1, 5, 9, tzinfo=UTC)  # a Monday
PARAMS = Params(half_life_weeks=1e18, min_samples=3, shrinkage=5.0)


def sample(
    n: int,
    working_seconds: float,
    *,
    stage: str = "wait_review",
    reviewer: str = "",
    ended: datetime | None = None,
    tail: float | None = 0.0,
    repo: str = "o/r",
    size: int = 3,
    round: int = 1,
) -> StageSample:
    end = ended or START + timedelta(days=n)
    return StageSample(
        task_key=f"o/r#{n}",
        stage=stage,
        started_at=end - timedelta(seconds=working_seconds),
        ended_at=end,
        seconds=working_seconds,
        reviewer=reviewer,
        repo=repo,
        size_bucket=size,
        round=round,
        finished_at=end + timedelta(seconds=tail) if tail is not None else None,
        tail_seconds=tail,
    )


def subject(**kw) -> Subject:
    kw.setdefault("stage", "wait_review")
    kw.setdefault("started_at", datetime(2026, 6, 1, 9, tzinfo=UTC))
    return Subject(**kw)


NOW = datetime(2026, 6, 1, 9, tzinfo=UTC)  # a Monday


class TestWorkingHours:
    def test_a_friday_evening_request_starts_on_monday(self):
        friday_6pm = datetime(2026, 10, 23, 18, tzinfo=UTC)
        monday_10 = datetime(2026, 10, 26, 10, tzinfo=UTC)
        assert UTC_HOURS.seconds(friday_6pm, monday_10) == HOUR

    def test_a_weekend_is_not_counted(self):
        assert (
            UTC_HOURS.seconds(
                datetime(2026, 10, 23, 16, tzinfo=UTC),
                datetime(2026, 10, 26, 10, tzinfo=UTC),
            )
            == 2 * HOUR
        )

    def test_advance_skips_nights_and_weekends(self):
        friday_4pm = datetime(2026, 10, 23, 16, tzinfo=UTC)
        assert UTC_HOURS.advance(friday_4pm, 2 * HOUR) == datetime(
            2026, 10, 26, 10, tzinfo=UTC
        )
        assert UTC_HOURS.advance(friday_4pm, 0) == friday_4pm

    def test_advance_inverts_seconds(self):
        begin = datetime(2026, 10, 21, 13, 30, tzinfo=UTC)
        end = UTC_HOURS.advance(begin, 3 * DAY + 2 * HOUR)
        assert UTC_HOURS.seconds(begin, end) == 3 * DAY + 2 * HOUR

    def test_hours_are_the_zones_not_utc(self):
        chicago = WorkingHours(tz=ZoneInfo("America/Chicago"))
        # 9:00 Central in October is 14:00 UTC.
        assert (
            chicago.seconds(
                datetime(2026, 10, 21, 14, tzinfo=UTC),
                datetime(2026, 10, 21, 15, tzinfo=UTC),
            )
            == HOUR
        )
        assert (
            chicago.seconds(
                datetime(2026, 10, 21, 8, tzinfo=UTC),
                datetime(2026, 10, 21, 13, tzinfo=UTC),
            )
            == 0
        )

    def test_config(self):
        hours = working_hours_from_config(
            {
                "days": ["Mon", "wed"],
                "start": "08:30",
                "end": "12:00",
                "timezone": "UTC",
            }
        )
        assert hours.days == (0, 2)
        assert hours.start == time(8, 30)
        assert hours.day_seconds == 3.5 * HOUR
        assert working_hours_from_config(None).days == (0, 1, 2, 3, 4)
        with pytest.raises(ValueError, match="unknown day"):
            working_hours_from_config({"days": ["funday"]})


class TestQuantiles:
    def test_exact_weighted_quantiles(self):
        pairs = [(float(v), 1.0) for v in range(1, 11)]
        assert weighted_quantile(pairs, 0.5) == 5.0
        assert weighted_quantile(pairs, 0.8) == 8.0
        assert weighted_quantile([(1.0, 1.0), (9.0, 3.0)], 0.5) == 9.0

    def test_size_bucket(self):
        assert [size_bucket(*p) for p in [(0, 0), (1, 0), (4, 4), (500, 12)]] == [
            0,
            0,
            3,
            9,
        ]


class TestEstimate:
    def test_no_history_is_unknown(self):
        result = estimate(History([]), subject(), NOW, PARAMS, UTC_HOURS)
        assert not result.known
        assert (result.p50_seconds, result.p80_seconds) == (None, None)
        assert result.level == "unknown"
        assert result.explanation.startswith("unknown: not enough history")

    def test_too_few_in_the_stage_falls_back_to_all_stages_then_unknown(self):
        few = [sample(1, HOUR), sample(2, 2 * HOUR)]
        assert not estimate(History(few), subject(), NOW, PARAMS, UTC_HOURS).known
        other = [sample(i, 4 * HOUR, stage="wait_ci") for i in range(3, 9)]
        result = estimate(History(few + other), subject(), NOW, PARAMS, UTC_HOURS)
        assert result.level == "global"
        assert result.partial
        assert result.p50_seconds == 4 * HOUR

    def test_stage_level_quantiles_are_exact(self):
        history = History([sample(i, i * HOUR, tail=0) for i in range(1, 11)])
        result = estimate(history, subject(), NOW, PARAMS, UTC_HOURS)
        assert result.level == "stage"
        assert result.n == 10
        assert (result.p50_seconds, result.p80_seconds) == (5 * HOUR, 8 * HOUR)
        assert not result.partial

    def test_reviewers_are_told_apart(self):
        fast = [sample(i, 2 * HOUR, reviewer="ann") for i in range(1, 31)]
        slow = [sample(i + 100, 2 * DAY, reviewer="bob") for i in range(1, 31)]
        history = History(fast + slow)
        ann = estimate(history, subject(reviewer="ann"), NOW, PARAMS, UTC_HOURS)
        bob = estimate(history, subject(reviewer="bob"), NOW, PARAMS, UTC_HOURS)
        assert ann.level.startswith("reviewer")
        assert ann.p50_seconds is not None and bob.p50_seconds is not None
        assert bob.p50_seconds > 5 * ann.p50_seconds
        assert "waiting on bob" in bob.explanation

    def test_shrinkage_pulls_a_small_group_to_the_stage_median(self):
        base = [sample(i, 2 * HOUR, reviewer="ann") for i in range(1, 31)]

        def bob(count: int) -> float:
            slow = [sample(i + 100, 2 * DAY, reviewer="bob") for i in range(count)]
            result = estimate(
                History(base + slow), subject(reviewer="bob"), NOW, PARAMS, UTC_HOURS
            )
            assert result.p50_seconds is not None
            return result.p50_seconds

        assert 2 * HOUR < bob(3) < bob(10) < bob(30) < 2 * DAY

    def test_a_reviewer_with_too_few_samples_uses_the_stage(self):
        history = History(
            [sample(i, 2 * HOUR, reviewer="ann") for i in range(1, 11)]
            + [sample(99, 3 * DAY, reviewer="bob")]
        )
        result = estimate(history, subject(reviewer="bob"), NOW, PARAMS, UTC_HOURS)
        assert result.level == "stage"

    def test_time_already_spent_conditions_the_remainder(self):
        history = History([sample(i, i * HOUR, tail=0) for i in range(1, 11)])
        started = NOW - timedelta(hours=4)  # 4 working hours in, same Monday...
        started = datetime(2026, 6, 1, 9, tzinfo=UTC)
        now = datetime(2026, 6, 1, 13, tzinfo=UTC)
        result = estimate(history, subject(started_at=started), now, PARAMS, UTC_HOURS)
        # Survivors last 5..10h; the remainders are 1..6h: p50 3h, p80 5h.
        assert result.elapsed_seconds == 4 * HOUR
        assert (result.p50_seconds, result.p80_seconds) == (3 * HOUR, 5 * HOUR)

    def test_overdue_when_no_sample_lasted_so_long(self):
        history = History([sample(i, 2 * HOUR, tail=0) for i in range(1, 8)])
        started = datetime(2026, 6, 1, 9, tzinfo=UTC)
        now = datetime(2026, 6, 3, 9, tzinfo=UTC)
        result = estimate(history, subject(started_at=started), now, PARAMS, UTC_HOURS)
        assert result.overdue
        assert result.p50_seconds == HOUR  # the floor
        assert "already longer than all of them" in result.explanation

    def test_the_remaining_stages_come_from_the_same_tasks(self):
        # Fast reviews are followed by a quick merge, slow ones by a slow one.
        fast = [sample(i, HOUR, tail=HOUR) for i in range(1, 11)]
        slow = [sample(i + 50, 3 * HOUR, tail=2 * DAY) for i in range(1, 11)]
        history = History(fast + slow)
        now = datetime(2026, 6, 1, 11, tzinfo=UTC)  # 2h in: only slow reviews survive
        result = estimate(
            history,
            subject(started_at=datetime(2026, 6, 1, 9, tzinfo=UTC)),
            now,
            PARAMS,
            UTC_HOURS,
        )
        assert result.p50_seconds == HOUR + 2 * DAY
        assert not result.partial

    def test_only_what_ended_before_the_instant_is_known(self):
        samples = [sample(i, HOUR) for i in range(1, 6)]
        history = History(samples)
        before_all = samples[0].ended_at
        assert not estimate(
            history, subject(), NOW, PARAMS, UTC_HOURS, known_at=before_all
        ).known
        later = estimate(
            history, subject(), NOW, PARAMS, UTC_HOURS, known_at=samples[3].ended_at
        )
        assert later.n == 3

    def test_recent_samples_count_more(self):
        old = [
            sample(i, 10 * HOUR, ended=datetime(2026, 1, 5 + i, 9, tzinfo=UTC))
            for i in range(10)
        ]
        new = [
            sample(i + 20, HOUR, ended=datetime(2026, 5, 20 + i, 9, tzinfo=UTC))
            for i in range(5)
        ]
        history = History(old + new)
        slow_forgetting = estimate(history, subject(), NOW, PARAMS, UTC_HOURS)
        fast_forgetting = estimate(
            history, subject(), NOW, Params(2.0, 3, 5.0), UTC_HOURS
        )
        assert slow_forgetting.p50_seconds == 10 * HOUR
        assert fast_forgetting.p50_seconds == HOUR


class TestStacked:
    def test_post_parent_tail_is_what_followed_blocked_stages(self):
        samples = [sample(i, DAY, stage="blocked", tail=3 * HOUR) for i in range(1, 6)]
        assert post_parent_tail(History(samples), PARAMS, NOW) == (3 * HOUR, 3 * HOUR)

    def test_post_parent_tail_falls_back_to_merge_durations(self):
        samples = [sample(i, 2 * HOUR, stage="merge") for i in range(1, 6)]
        assert post_parent_tail(History(samples), PARAMS, NOW) == (2 * HOUR, 2 * HOUR)
        assert post_parent_tail(History([]), PARAMS, NOW) is None


def synthetic(
    tasks: int,
    *,
    review: float,
    seed: int = 1,
    first_week: int = 0,
    jitter: float = 0.5,
) -> list[StageSample]:
    """Tasks each opened a day apart: a review wait around ``review`` working
    seconds, then CI and a merge. Lognormal-ish noise from a seeded RNG."""
    rng = random.Random(seed)
    out = []
    for i in range(tasks):
        t = START + timedelta(weeks=first_week, days=i * 1.0)
        t = UTC_HOURS.advance(t, 0)
        wait = review * rng.uniform(1 - jitter, 1 + jitter)
        ci = HOUR * rng.uniform(0.5, 1.5)
        merge = HOUR * rng.uniform(0.5, 1.5)
        review_end = UTC_HOURS.advance(t, wait)
        ci_end = UTC_HOURS.advance(review_end, ci)
        merge_end = UTC_HOURS.advance(ci_end, merge)
        for stage, a, b, d in (
            ("wait_review", t, review_end, wait),
            ("wait_ci", review_end, ci_end, ci),
            ("merge", ci_end, merge_end, merge),
        ):
            out.append(
                StageSample(
                    task_key=f"o/r#{first_week}-{i}",
                    stage=stage,
                    started_at=a,
                    ended_at=b,
                    seconds=d,
                    repo="o/r",
                    size_bucket=3,
                    finished_at=merge_end,
                    tail_seconds=UTC_HOURS.seconds(b, merge_end),
                )
            )
    return out


class TestBacktest:
    def test_coverage_is_near_the_target(self):
        samples = synthetic(160, review=DAY, jitter=0.9, seed=7)
        outcomes = replay(samples, UTC_HOURS, Params(), max_tasks=100)
        overall = metrics_of(outcomes)
        assert overall.coverage is not None
        assert 0.7 <= overall.coverage <= 0.95
        assert overall.n == len(outcomes)
        assert overall.unknown < overall.n // 3

    def test_a_prediction_never_sees_its_own_future(self):
        samples = synthetic(40, review=DAY, seed=3)
        history = History(samples)
        first = min(samples, key=lambda s: s.started_at)
        guess = estimate(
            history,
            Subject(first.stage, first.started_at),
            first.started_at,
            Params(),
            UTC_HOURS,
            known_at=first.started_at,
        )
        assert not guess.known

    def test_the_estimate_follows_a_regime_change(self):
        old = synthetic(60, review=2 * DAY, seed=1)
        new = synthetic(30, review=DAY, seed=2, first_week=14)
        history = History(old + new)
        when = max(s.ended_at for s in new) + timedelta(days=1)
        quick = Params(half_life_weeks=2.0, min_samples=3, shrinkage=5.0)
        result = estimate(history, Subject("wait_review", when), when, quick, UTC_HOURS)
        assert result.p50_seconds is not None
        assert result.p50_seconds < 1.3 * DAY

    def test_tuning_picks_a_shorter_half_life_after_a_regime_change(self):
        stable = synthetic(120, review=DAY, seed=5)
        shifted = synthetic(60, review=2 * DAY, seed=6, first_week=24) + synthetic(
            60, review=0.5 * DAY, seed=8, first_week=48
        )
        grid = [Params(h, 3, 5.0) for h in (52.0, 12.0, 2.0)]
        stable_best = run_backtest(
            stable, UTC_HOURS, START + timedelta(weeks=60), grid=grid
        )
        shifted_best = run_backtest(
            stable + shifted,
            UTC_HOURS,
            START + timedelta(weeks=90),
            grid=grid,
            max_tasks=100,
        )
        assert (
            shifted_best.params.half_life_weeks < stable_best.params.half_life_weeks
            or (shifted_best.params.half_life_weeks < 52.0)
        )

    def test_unknown_is_scored_as_no_time_left(self):
        samples = synthetic(3, review=DAY)
        outcomes = replay(samples, UTC_HOURS, Params(min_samples=50))
        assert all(o.p50 is None for o in outcomes)
        assert loss(outcomes) > 0
        assert metrics_of(outcomes).coverage is None

    def test_regime_change_is_reported_with_its_size(self):
        now = START + timedelta(weeks=30)
        older = [
            sample(i, 8 * HOUR, ended=now - timedelta(weeks=6, days=i))
            for i in range(10)
        ]
        newer = [
            sample(i + 20, 4 * HOUR, ended=now - timedelta(weeks=2, days=i))
            for i in range(10)
        ]
        found = regime_changes(older + newer, now)
        assert len(found) == 1
        assert found[0].stage == "wait_review"
        assert found[0].change == pytest.approx(-0.5)
        assert (found[0].n_recent, found[0].n_prior) == (10, 10)
        assert regime_changes(older + newer[:5], now) == []  # too few to say


class TestRobustness:
    def test_instant_stages_do_not_blow_up_the_slow_ones(self):
        # Most reviewers are instant, one is slow: a task with the slow
        # reviewer is told so, but nothing is scaled beyond the limit.
        instant = [sample(i, 0.0, reviewer="ann") for i in range(1, 21)]
        slow = [sample(i + 50, 8 * HOUR, reviewer="bob") for i in range(1, 6)]
        history = History(instant + slow)

        result = estimate(history, subject(reviewer="ann"), NOW, PARAMS, UTC_HOURS)

        assert result.p80_seconds is not None
        assert result.p80_seconds <= 10 * 8 * HOUR

    def test_every_grid_point_is_scored_and_the_first_of_equals_wins(self):
        from toolbelt.finish_estimate.backtest import tune

        samples = synthetic(30, review=DAY, seed=4)
        grid = [Params(12.0, 3, 5.0), Params(12.0, 3, 5.0)]

        best, best_loss, scored, _ = tune(samples, UTC_HOURS, grid=grid)

        assert best is grid[0]
        assert [value for _, value in scored] == [best_loss, best_loss]
