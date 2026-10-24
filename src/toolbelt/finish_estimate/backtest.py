"""Backtesting: replay the history, predicting every finished task at each of
its stage boundaries from only what was known strictly before that instant,
and compare with the actual finish. The same replay picks the estimator's
hyperparameters from a small fixed grid by pinball loss, and spots a change
in how long stages take."""

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from toolbelt.finish_estimate.model import (
    PARAMS_GRID,
    History,
    Params,
    StageSample,
    Subject,
    estimate,
)
from toolbelt.finish_estimate.working_hours import WorkingHours

#: How many of the most recently finished tasks are replayed.
MAX_TASKS = 150

#: The quantiles the estimate states and the loss scores.
QUANTILES = (0.5, 0.8)

#: A prediction of "unknown" is scored as if it had predicted no time left, so
#: a setting cannot win by declining to answer.
UNKNOWN_PREDICTION = 0.0

#: A change of the median stage duration beyond this share is a regime change,
#: with at least this many samples in each window.
REGIME_THRESHOLD = 0.3
REGIME_MIN_SAMPLES = 8
REGIME_WEEKS = 4


@dataclass(frozen=True)
class Outcome:
    """One backtested prediction. Seconds are working seconds left at
    ``made_at``."""

    task_key: str
    stage: str
    made_at: datetime
    actual: float
    p50: float | None
    p80: float | None
    level: str
    n: int


def _subject(sample: StageSample) -> Subject:
    return Subject(
        stage=sample.stage,
        started_at=sample.started_at,
        reviewer=sample.reviewer,
        repo=sample.repo,
        size_bucket=sample.size_bucket,
        round=sample.round,
    )


def replay(
    samples: Sequence[StageSample],
    hours: WorkingHours,
    params: Params,
    *,
    max_tasks: int = MAX_TASKS,
    history: History | None = None,
) -> list[Outcome]:
    """Predicts, at the start of each stage of the ``max_tasks`` most recently
    finished tasks, when the task would finish, from the samples that had
    ended (and tasks that had finished) before that instant."""
    history = history or History(samples)
    finished: dict[str, datetime] = {}
    for sample in samples:
        if sample.finished_at is not None:
            finished[sample.task_key] = sample.finished_at
    recent = {
        key
        for key, _ in sorted(finished.items(), key=lambda kv: (kv[1], kv[0]))[
            -max_tasks:
        ]
    }
    outcomes = []
    for sample in sorted(samples, key=lambda s: (s.started_at, s.task_key)):
        if sample.task_key not in recent:
            continue
        end = finished[sample.task_key]
        if sample.started_at >= end:
            continue
        guess = estimate(history, _subject(sample), sample.started_at, params, hours)
        outcomes.append(
            Outcome(
                task_key=sample.task_key,
                stage=sample.stage,
                made_at=sample.started_at,
                actual=hours.seconds(sample.started_at, end),
                p50=guess.p50_seconds,
                p80=guess.p80_seconds,
                level=guess.level,
                n=guess.n,
            )
        )
    return outcomes


def pinball(actual: float, predicted: float, q: float) -> float:
    gap = actual - predicted
    return q * gap if gap >= 0 else (q - 1) * gap


def loss(outcomes: Sequence[Outcome]) -> float:
    """The mean pinball loss over both quantiles, in seconds; ``inf`` with no
    outcome."""
    if not outcomes:
        return float("inf")
    total = 0.0
    for o in outcomes:
        for q, predicted in zip(QUANTILES, (o.p50, o.p80), strict=True):
            total += pinball(
                o.actual, UNKNOWN_PREDICTION if predicted is None else predicted, q
            )
    return total / (len(outcomes) * len(QUANTILES))


@dataclass(frozen=True)
class StageMetrics:
    stage: str
    #: Predictions made, and how many were "unknown".
    n: int
    unknown: int
    #: The share of the known ones whose actual time was at or under p80
    #: (the target is 0.8); ``None`` with none known.
    coverage: float | None
    median_abs_error_seconds: float | None
    #: The median of p80 - p50.
    median_width_seconds: float | None


def metrics_of(outcomes: Sequence[Outcome], stage: str = "all") -> StageMetrics:
    known = [o for o in outcomes if o.p50 is not None and o.p80 is not None]
    return StageMetrics(
        stage=stage,
        n=len(outcomes),
        unknown=len(outcomes) - len(known),
        coverage=(
            sum(o.actual <= o.p80 for o in known if o.p80 is not None) / len(known)
            if known
            else None
        ),
        median_abs_error_seconds=(
            statistics.median(abs(o.actual - o.p50) for o in known if o.p50 is not None)
            if known
            else None
        ),
        median_width_seconds=(
            statistics.median(
                o.p80 - o.p50 for o in known if o.p50 is not None and o.p80 is not None
            )
            if known
            else None
        ),
    )


def metrics_by_stage(outcomes: Sequence[Outcome]) -> list[StageMetrics]:
    grouped: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        grouped[o.stage].append(o)
    return [metrics_of(grouped[stage], stage) for stage in sorted(grouped)]


@dataclass(frozen=True)
class RegimeChange:
    """A stage whose median duration moved: ``change`` is the recent window's
    median over the prior window's, minus one (-0.4: 40% shorter)."""

    stage: str
    weeks: int
    recent_median_seconds: float
    prior_median_seconds: float
    change: float
    n_recent: int
    n_prior: int


def regime_changes(
    samples: Sequence[StageSample],
    now: datetime,
    *,
    weeks: int = REGIME_WEEKS,
    threshold: float = REGIME_THRESHOLD,
    min_samples: int = REGIME_MIN_SAMPLES,
) -> list[RegimeChange]:
    """Stages whose last ``weeks`` of durations differ from the ``weeks``
    before them by more than ``threshold`` (needing ``min_samples`` in each)."""
    edge = now - timedelta(weeks=weeks)
    before = now - timedelta(weeks=2 * weeks)
    recent: dict[str, list[float]] = defaultdict(list)
    prior: dict[str, list[float]] = defaultdict(list)
    for s in samples:
        if edge < s.ended_at <= now:
            recent[s.stage].append(s.seconds)
        elif before < s.ended_at <= edge:
            prior[s.stage].append(s.seconds)
    found = []
    for stage in sorted(recent):
        if len(recent[stage]) < min_samples or len(prior.get(stage, ())) < min_samples:
            continue
        now_median = statistics.median(recent[stage])
        was = statistics.median(prior[stage])
        if was <= 0:
            continue
        change = now_median / was - 1
        if abs(change) > threshold:
            found.append(
                RegimeChange(
                    stage=stage,
                    weeks=weeks,
                    recent_median_seconds=now_median,
                    prior_median_seconds=was,
                    change=change,
                    n_recent=len(recent[stage]),
                    n_prior=len(prior[stage]),
                )
            )
    return found


@dataclass(frozen=True)
class BacktestResult:
    params: Params
    loss: float
    #: The loss of every grid point, in grid order.
    grid: tuple[tuple[Params, float], ...]
    overall: StageMetrics
    by_stage: tuple[StageMetrics, ...]
    regime: tuple[RegimeChange, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return asdict(self)


def tune(
    samples: Sequence[StageSample],
    hours: WorkingHours,
    *,
    grid: Sequence[Params] = PARAMS_GRID,
    max_tasks: int = MAX_TASKS,
) -> tuple[Params, float, list[tuple[Params, float]], list[Outcome]]:
    """The grid point with the lowest pinball loss (the first of equals), its
    loss, every point's loss and the best point's outcomes."""
    history = History(samples)
    scored = []
    best: tuple[Params, float, list[Outcome]] | None = None
    for params in grid:
        outcomes = replay(samples, hours, params, max_tasks=max_tasks, history=history)
        value = loss(outcomes)
        scored.append((params, value))
        if best is None or value < best[1]:
            best = (params, value, outcomes)
    assert best is not None
    return best[0], best[1], scored, best[2]


def run_backtest(
    samples: Sequence[StageSample],
    hours: WorkingHours,
    now: datetime,
    *,
    grid: Sequence[Params] = PARAMS_GRID,
    max_tasks: int = MAX_TASKS,
) -> BacktestResult:
    params, best_loss, scored, outcomes = tune(
        samples, hours, grid=grid, max_tasks=max_tasks
    )
    return BacktestResult(
        params=params,
        loss=best_loss,
        grid=tuple(scored),
        overall=metrics_of(outcomes),
        by_stage=tuple(metrics_by_stage(outcomes)),
        regime=tuple(regime_changes(samples, now)),
    )
