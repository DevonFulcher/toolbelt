"""Backtesting: replay the history, predicting at every checkpoint of every
task from only what was known strictly before it, and compare with what
happened. Any ``ForecastModel`` runs through the same harness on the same
splits, so models are compared on identical rows and a feature can be taken in
and out (``ablate``). The harness also picks the champion per stage and spots
a change in how long stages take."""

import statistics
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from toolbelt.finish_estimate.forecast import ForecastModel, TrainingData
from toolbelt.finish_estimate.model import StageSample
from toolbelt.finish_estimate.snapshots import (
    SnapshotRow,
    TaskTimeline,
    build_snapshots,
    samples_of_timelines,
    with_known_at,
)
from toolbelt.finish_estimate.working_hours import WorkingHours

#: How many of the most recently ended tasks are tested (every task trains).
MAX_TASKS = 150

#: The model refits at most this often during the replay (it is still fitted
#: only on what was known before the instant it was fitted at).
REFIT_EVERY = timedelta(days=1)

#: The quantiles the forecast states and the loss scores.
QUANTILES = (0.5, 0.8)

#: A prediction of "unknown" is scored as if it had predicted no time left, so
#: a model cannot win by declining to answer.
UNKNOWN_PREDICTION = 0.0

#: Champion rules (``choose_champions``).
CHAMPION_MARGIN = 0.05
CHAMPION_MIN_POINTS = 30
BASELINE = "baseline"

#: A change of the median stage duration beyond this share is a regime change,
#: with at least this many samples in each window.
REGIME_THRESHOLD = 0.3
REGIME_MIN_SAMPLES = 8
REGIME_WEEKS = 4


@dataclass(frozen=True)
class Outcome:
    """One backtested prediction. Seconds are working seconds left at
    ``made_at``; ``censored``: the task was still open, so ``actual`` is only
    a lower bound."""

    task_key: str
    stage: str
    made_at: datetime
    actual: float
    censored: bool
    p50: float | None
    p80: float | None
    level: str
    n: int


def pinball(actual: float, predicted: float, q: float) -> float:
    gap = actual - predicted
    return q * gap if gap >= 0 else (q - 1) * gap


def loss(outcomes: Sequence[Outcome]) -> float:
    """The mean pinball loss over both quantiles of the outcomes whose finish
    is known (censored ones have no actual to score), in seconds; ``inf``
    with none."""
    scored = [o for o in outcomes if not o.censored]
    if not scored:
        return float("inf")
    total = 0.0
    for o in scored:
        for q, predicted in zip(QUANTILES, (o.p50, o.p80), strict=True):
            total += pinball(
                o.actual, UNKNOWN_PREDICTION if predicted is None else predicted, q
            )
    return total / (len(scored) * len(QUANTILES))


@dataclass(frozen=True)
class StageMetrics:
    stage: str
    #: Predictions made, how many were "unknown" and how many were for tasks
    #: still open (right-censored).
    n: int
    unknown: int
    censored: int
    #: The share of the known, finished-task predictions whose actual time was
    #: at or under the p80 (the target is 0.8); ``None`` with none.
    coverage: float | None
    #: The same with every open task already past its p80 counted as a miss
    #: (it is one, whatever happens next).
    coverage_adjusted: float | None
    median_abs_error_seconds: float | None
    #: The median of p80 - p50.
    median_width_seconds: float | None
    #: Mean pinball loss (see ``loss``); ``None`` with no finished task.
    loss: float | None


def metrics_of(outcomes: Sequence[Outcome], stage: str = "all") -> StageMetrics:
    done = [o for o in outcomes if not o.censored]
    known = [o for o in done if o.p50 is not None and o.p80 is not None]
    over = [
        o for o in outcomes if o.censored and o.p80 is not None and o.actual > o.p80
    ]
    hits = sum(o.actual <= o.p80 for o in known if o.p80 is not None)
    scored = loss(outcomes)
    return StageMetrics(
        stage=stage,
        n=len(outcomes),
        unknown=sum(o.p50 is None for o in outcomes),
        censored=sum(o.censored for o in outcomes),
        coverage=hits / len(known) if known else None,
        coverage_adjusted=hits / (len(known) + len(over)) if known else None,
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
        loss=None if scored == float("inf") else scored,
    )


def metrics_by_stage(outcomes: Sequence[Outcome]) -> list[StageMetrics]:
    grouped: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        grouped[o.stage].append(o)
    return [metrics_of(grouped[stage], stage) for stage in sorted(grouped)]


# ---- the replay -------------------------------------------------------------------------


def training_data(
    samples: Sequence[StageSample],
    rows: Sequence[SnapshotRow],
    checkpoints: Sequence[datetime],
    known_at: datetime,
    hours: WorkingHours,
) -> TrainingData:
    """What may be learned from at ``known_at``: stages ended before it, and
    the rows checkpointed before it with their targets censored at it where
    they were not known yet. ``checkpoints`` are ``rows``' instants, sorted."""
    end = bisect_left(checkpoints, known_at)
    return TrainingData(
        known_at=known_at,
        hours=hours,
        samples=[s for s in samples if s.ended_at < known_at],
        rows=[with_known_at(r, known_at, hours) for r in rows[:end]],
    )


def tested_tasks(timelines: Sequence[TaskTimeline], max_tasks: int) -> set[str]:
    """The ``max_tasks`` most recent tasks by their last activity."""

    def last(task: TaskTimeline) -> datetime:
        times = [span.ended_at or span.started_at for span in task.spans]
        if task.ended_at:
            times.append(task.ended_at)
        return max(times, default=datetime.min.replace(tzinfo=UTC))

    ranked = sorted(timelines, key=lambda t: (last(t), t.task_key))
    return {t.task_key for t in ranked[-max_tasks:]}


def replay(
    models: Mapping[str, ForecastModel],
    timelines: Sequence[TaskTimeline],
    hours: WorkingHours,
    now: datetime,
    *,
    max_tasks: int = MAX_TASKS,
    refit_every: timedelta = REFIT_EVERY,
) -> dict[str, list[Outcome]]:
    """Runs every model over the same rows: at each checkpoint of the tested
    tasks it is (re)fitted on what was known strictly before and predicts the
    finish. Rows of a task that was closed without merging have no finish and
    are not scored; rows of one still open are kept, censored."""
    samples = samples_of_timelines(timelines, hours)
    rows = build_snapshots(timelines, hours, now)
    checkpoints = [r.checkpoint for r in rows]
    tested = tested_tasks(timelines, max_tasks)
    out: dict[str, list[Outcome]] = {name: [] for name in models}
    next_fit: datetime | None = None
    for row in rows:
        if row.features.task_key not in tested or row.finish_remaining_seconds is None:
            continue
        if next_fit is None or row.checkpoint >= next_fit:
            data = training_data(samples, rows, checkpoints, row.checkpoint, hours)
            for model in models.values():
                model.fit(data)
            next_fit = row.checkpoint + refit_every
        for name, model in models.items():
            guess = model.predict(row.features)
            out[name].append(
                Outcome(
                    task_key=row.features.task_key,
                    stage=row.features.stage,
                    made_at=row.checkpoint,
                    actual=row.finish_remaining_seconds,
                    censored=row.finish_censored,
                    p50=guess.p50_seconds,
                    p80=guess.p80_seconds,
                    level=guess.level,
                    n=guess.n,
                )
            )
    return out


# ---- champions ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One model's record on a stage's test points."""

    model: str
    points: int
    loss: float | None
    coverage: float | None


@dataclass(frozen=True)
class Champion:
    """The model that answers for a stage, and why: ``margin`` is how much its
    loss beats the baseline's (0 when the baseline stays)."""

    stage: str
    model: str
    margin: float
    points: int
    evidence: tuple[Evidence, ...]


def choose_champions(
    outcomes: Mapping[str, Sequence[Outcome]],
    *,
    margin: float = CHAMPION_MARGIN,
    min_points: int = CHAMPION_MIN_POINTS,
) -> dict[str, Champion]:
    """Per stage (and ``all``), the model with the lowest pinball loss when it
    beats the baseline's by at least ``margin`` (a share of it) on at least
    ``min_points`` scored test points; otherwise the baseline stays."""
    stages = sorted({o.stage for rows in outcomes.values() for o in rows})
    result: dict[str, Champion] = {}
    for stage in ("all", *stages):
        evidence = []
        values: dict[str, float] = {}
        points = 0
        for model, rows in outcomes.items():
            picked = [
                o
                for o in rows
                if (stage == "all" or o.stage == stage) and not o.censored
            ]
            m = metrics_of(picked, stage)
            evidence.append(Evidence(model, len(picked), m.loss, m.coverage))
            if m.loss is not None:
                values[model] = m.loss
            points = max(points, len(picked))
        chosen, gain = BASELINE, 0.0
        base = values.get(BASELINE)
        if base is not None and points >= min_points:
            challengers = {k: v for k, v in values.items() if k != BASELINE}
            if challengers:
                best = min(sorted(challengers), key=lambda k: challengers[k])
                share = (base - challengers[best]) / base if base > 0 else 0.0
                if share >= margin:
                    chosen, gain = best, share
        result[stage] = Champion(stage, chosen, gain, points, tuple(evidence))
    return result


# ---- ablation --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ablation:
    """The change in pinball loss when ``feature`` is taken out of a model:
    positive ``delta`` means the model got worse without it (the feature is
    signal), around zero or negative means it is noise."""

    feature: str
    loss_with: float | None
    loss_without: float | None
    delta: float | None
    points: int


def ablate(
    make: Callable[[frozenset[str]], ForecastModel],
    features: Sequence[str],
    timelines: Sequence[TaskTimeline],
    hours: WorkingHours,
    now: datetime,
    *,
    max_tasks: int = MAX_TASKS,
    refit_every: timedelta = timedelta(days=3),
) -> list[Ablation]:
    """Runs ``make(excluded)`` with every feature in, then each one out, on the
    same rows and splits."""
    models = {"": make(frozenset())} | {f: make(frozenset({f})) for f in features}
    outcomes = replay(
        models, timelines, hours, now, max_tasks=max_tasks, refit_every=refit_every
    )
    full = metrics_of(outcomes[""]).loss
    rows = []
    for feature in features:
        without = metrics_of(outcomes[feature]).loss
        rows.append(
            Ablation(
                feature=feature,
                loss_with=full,
                loss_without=without,
                delta=None if full is None or without is None else without - full,
                points=sum(not o.censored for o in outcomes[feature]),
            )
        )
    return rows


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
