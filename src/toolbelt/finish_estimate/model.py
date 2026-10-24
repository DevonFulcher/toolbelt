"""The estimator: pure functions from a history of stage durations to the
working time a task has left, as a median (p50) and a p80.

Per stage (the kinds of ``next_action``), it keeps the recency-weighted
distribution of how long that stage took, in working time. Groups (a
reviewer, a size bucket, a repo, a review round) scale it by a factor learned
by shrinkage, so a group with few samples is pulled to the stage's median. The
time left in the stage the task is in is conditioned on the time already spent
in it (only samples that lasted longer count), and the stages after it are the
time those same historical tasks still needed once the stage ended, so what
makes tasks slow together stays together. Nothing is random: quantiles are
taken from sorted values. See ``docs/finish-estimate.md``.
"""

import math
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from toolbelt.finish_estimate.features import BY_NAME, DEFAULT_DIMS, STAGE_DIMS, Value
from toolbelt.finish_estimate.working_hours import WorkingHours

#: Bumped when the model changes what it predicts for the same history, so
#: stored predictions of an older one are not compared with a newer one's.
MODEL_VERSION = 1

#: The longest history one stage keeps (its most recent samples), and from
#: every stage for the "global" level.
STAGE_CAP = 300
GLOBAL_CAP = 50

#: Fewest survivors with a known remainder for the joint estimate; below it the
#: later stages are added as their own quantiles.
MIN_JOINT = 3

#: Durations are clamped to this many seconds before taking a logarithm.
_FLOOR_SECONDS = 300.0

#: A sample is scaled to the task's groups by at most this factor either way,
#: so a stage that is often instant (a median of nothing) cannot blow up the
#: ones that are not.
MAX_ADJUSTMENT = 10.0

#: The answer for a stage that has already lasted longer than any sample is a
#: fraction of its typical duration, at least this long.
OVERDUE_FRACTION = 0.25
OVERDUE_MIN_SECONDS = 3600.0

_PHRASES = {
    "wait_review": "waiting on {reviewer}",
    "address_review": "addressing {reviewer}'s review",
    "wait_ci": "waiting on CI",
    "fix_ci": "fixing CI",
    "blocked": "blocked on its parent",
    "request_review": "about to request review",
    "personal_review": "waiting on your personal review",
    "merge": "ready to merge",
    "open_pr": "before the PR is opened",
    "resolve_conflicts": "resolving conflicts",
    "resolve_comments": "resolving comments",
}
_NOUNS = {"wait_review": "reviews", "wait_ci": "CI runs", "merge": "merges"}


@dataclass(frozen=True)
class Params:
    """What backtesting tunes."""

    #: A sample half as old as this counts half as much.
    half_life_weeks: float = 12.0
    #: The fewest samples a group (or a stage) needs to be used.
    min_samples: int = 5
    #: The pseudo-count of the shrinkage: a group's factor is weighted
    #: ``n / (n + shrinkage)``.
    shrinkage: float = 5.0


DEFAULT_PARAMS = Params()


@dataclass(frozen=True)
class StageSample:
    """One stage of one task that has ended."""

    task_key: str
    stage: str
    started_at: datetime
    ended_at: datetime
    #: The stage's length in working seconds.
    seconds: float
    reviewer: str = ""
    repo: str = ""
    size_bucket: int = 0
    round: int = 1
    #: When the task finished (merged), and the working seconds from this
    #: stage's end to then; ``None`` for a task that did not finish.
    finished_at: datetime | None = None
    tail_seconds: float | None = None

    def value(self, dim: str) -> str:
        """The categorical feature ``dim`` of the stage (a stored column:
        ``features.REGISTRY``); ``""`` when it has none."""
        attr = BY_NAME[dim].sample_attr
        assert attr is not None, dim
        value = getattr(self, attr)
        return "" if value in (None, "") else str(value)


@dataclass(frozen=True)
class Subject:
    """A stage a task is in (or entering), to estimate the rest of. ``values``
    are its registry features by name (``features.REGISTRY``)."""

    stage: str
    started_at: datetime
    values: Mapping[str, Value] = field(default_factory=dict)

    def value(self, dim: str) -> str:
        value = self.values.get(dim)
        return "" if value in (None, "") else str(value)

    @property
    def reviewer(self) -> str:
        return self.value("reviewer")


def size_bucket(additions: int, deletions: int) -> int:
    """log2 of the lines changed (0 for none or one)."""
    return int(math.log2(max(1, additions + deletions)))


#: The fixed quantile grid a finish distribution is carried on: 19 levels.
LEVELS: tuple[float, ...] = tuple(i / 20 for i in range(1, 20))


@dataclass(frozen=True)
class Prior:
    """What to answer for a stage with no history of its own: its typical
    working seconds at p50 and p80 (configured: ``estimate.priors``)."""

    p50_seconds: float
    p80_seconds: float


@dataclass(frozen=True)
class FactorInfo:
    """One factor applied to the estimate: the group of the task (a reviewer,
    a size bucket, ...), its samples, and its raw and shrunk factors."""

    dim: str
    value: str
    n: int
    raw: float
    shrunk: float


@dataclass(frozen=True)
class Estimate:
    """The working time left. ``p50_seconds`` / ``p80_seconds`` are ``None``
    when ``level`` is ``unknown``: a number is never invented."""

    stage: str
    p50_seconds: float | None
    p80_seconds: float | None
    #: Which fallback answered: ``reviewer+size``, ``reviewer``, ``size``, ...
    #: ``stage``, ``global`` or ``unknown``.
    level: str
    #: The samples it is based on.
    n: int
    #: The stage has already lasted longer than every sample.
    overdue: bool
    #: The stages after this one are not in the numbers (no task finished
    #: after a comparable stage yet).
    partial: bool
    elapsed_seconds: float
    reviewer: str
    explanation: str
    #: The remaining time to finish at ``LEVELS`` (``None``: not carried).
    grid: tuple[float, ...] | None = None
    factors: tuple[FactorInfo, ...] = ()
    #: The past stages most like this one (same groups, most recent first).
    similar: tuple[StageSample, ...] = ()

    @property
    def known(self) -> bool:
        return self.p50_seconds is not None


@dataclass
class History:
    """The samples, by stage and in the order they ended."""

    samples: Sequence[StageSample]
    _by_stage: dict[str, list[StageSample]] = field(init=False, repr=False)
    _ended: dict[str, list[datetime]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        grouped: dict[str, list[StageSample]] = defaultdict(list)
        for sample in sorted(self.samples, key=lambda s: (s.ended_at, s.task_key)):
            grouped[sample.stage].append(sample)
        self._by_stage = dict(grouped)
        self._ended = {
            stage: [s.ended_at for s in items] for stage, items in grouped.items()
        }

    def stages(self) -> list[str]:
        return sorted(self._by_stage)

    def before(self, stage: str, known_at: datetime, cap: int) -> list[StageSample]:
        """The ``cap`` latest samples of ``stage`` that ended strictly before
        ``known_at``."""
        items = self._by_stage.get(stage, [])
        end = bisect_left(self._ended.get(stage, []), known_at)
        return items[max(0, end - cap) : end]


# ---- weighted statistics -----------------------------------------------------------


def weighted_quantile(pairs: Iterable[tuple[float, float]], q: float) -> float:
    """The smallest value whose cumulative weight reaches ``q`` of the total
    (``(value, weight)`` pairs; no interpolation)."""
    ordered = sorted(pairs)
    total = sum(weight for _, weight in ordered)
    target = q * total
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= target - 1e-12:
            return value
    return ordered[-1][0]


def _weights(samples: Sequence[StageSample], known_at: datetime, half_life: float):
    week = 7 * 86400.0
    return [
        0.5 ** (max(0.0, (known_at - s.ended_at).total_seconds()) / week / half_life)
        for s in samples
    ]


def _log_median(samples: Sequence[StageSample], weights: Sequence[float]) -> float:
    return math.log(
        max(
            weighted_quantile(
                zip((s.seconds for s in samples), weights, strict=True), 0.5
            ),
            _FLOOR_SECONDS,
        )
    )


def format_working(seconds: float, hours: WorkingHours) -> str:
    """Working time for the explanation: minutes, hours or working days."""
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    if seconds < hours.day_seconds:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / hours.day_seconds:.1f} working days"


def _factors(
    samples: Sequence[StageSample],
    weights: Sequence[float],
    dims: Sequence[str],
    params: Params,
) -> dict[str, dict[str, tuple[float, float, int]]]:
    """Per dimension, each group's ``(raw, shrunk, n)`` factor. Raw is
    ``median_group / median_stage``: dividing a sample by its groups' raw
    factors leaves only its spread within the groups. Shrunk is ``exp(n/(n+k)
    * (log median_group - log median_stage))``: what a task of the group is
    scaled by, pulled to the stage's median when ``n`` is small. Groups of
    fewer than ``min_samples`` have none."""
    stage_log = _log_median(samples, weights)
    out: dict[str, dict[str, tuple[float, float, int]]] = {}
    for dim in dims:
        groups: dict[str, list[int]] = defaultdict(list)
        for index, sample in enumerate(samples):
            value = sample.value(dim)
            if value != "":
                groups[value].append(index)
        out[dim] = {}
        for value, indexes in groups.items():
            if len(indexes) < params.min_samples:
                continue
            n = len(indexes)
            members = [samples[i] for i in indexes]
            log_group = _log_median(members, [weights[i] for i in indexes])
            out[dim][value] = (
                math.exp(log_group - stage_log),
                math.exp(n / (n + params.shrinkage) * (log_group - stage_log)),
                n,
            )
    return out


def _unknown(subject: Subject, elapsed: float, n: int, params: Params) -> Estimate:
    return Estimate(
        stage=subject.stage,
        p50_seconds=None,
        p80_seconds=None,
        level="unknown",
        n=n,
        overdue=False,
        partial=False,
        elapsed_seconds=elapsed,
        reviewer=subject.reviewer,
        explanation=(
            f"unknown: no history for this stage yet ({n} of the "
            f"{params.min_samples} {subject.stage.replace('_', ' ')} stages needed)"
        ),
    )


def estimate(
    history: History,
    subject: Subject,
    now: datetime,
    params: Params,
    hours: WorkingHours,
    *,
    known_at: datetime | None = None,
    prior: Prior | None = None,
) -> Estimate:
    """The working time left for a task in ``subject``'s stage at ``now``,
    from what was known at ``known_at`` (default ``now``; a backtest passes the
    instant it predicts from). A sample counts once its stage ended before
    ``known_at``, and its remainder only once the task finished before it. A
    stage with fewer than ``min_samples`` samples is unknown (it is never
    answered from other stages), unless ``prior`` says what to expect."""
    known_at = known_at or now
    elapsed = hours.seconds(subject.started_at, now)
    pool = history.before(subject.stage, known_at, STAGE_CAP)
    dims = STAGE_DIMS.get(subject.stage, DEFAULT_DIMS)
    used: list[str] = []
    info: list[FactorInfo] = []
    factor_of: dict[int, float] = {}
    target_factor = 1.0
    level = "stage"
    if len(pool) < params.min_samples:
        if prior is None:
            return _unknown(subject, elapsed, len(pool), params)
        floor = OVERDUE_MIN_SECONDS
        p50 = max(prior.p50_seconds - elapsed, floor)
        p80 = max(prior.p80_seconds - elapsed, p50)
        return Estimate(
            stage=subject.stage,
            p50_seconds=p50,
            p80_seconds=p80,
            level="prior",
            n=len(pool),
            overdue=False,
            partial=True,
            elapsed_seconds=elapsed,
            reviewer=subject.reviewer,
            explanation=(
                f"{subject.stage.replace('_', ' ')}: a configured prior, only "
                f"{len(pool)} stages of history"
            ),
        )
    weights = _weights(pool, known_at, params.half_life_weeks)
    if dims:
        factors = _factors(pool, weights, dims, params)
        for dim in dims:
            wanted = subject.value(dim)
            # A dimension every sample shares tells nothing.
            if (
                wanted != ""
                and wanted in factors[dim]
                and len({s.value(dim) for s in pool}) > 1
            ):
                used.append(dim)
                target_factor *= factors[dim][wanted][1]
                info.append(
                    FactorInfo(
                        dim,
                        wanted,
                        factors[dim][wanted][2],
                        factors[dim][wanted][0],
                        factors[dim][wanted][1],
                    )
                )
        for index, sample in enumerate(pool):
            product = 1.0
            for dim in dims:
                product *= factors[dim].get(sample.value(dim), (1.0, 1.0))[0]
            factor_of[index] = product
        if used:
            level = "+".join(used)

    adjusted = [
        s.seconds
        * min(
            MAX_ADJUSTMENT,
            max(1 / MAX_ADJUSTMENT, target_factor / factor_of.get(i, 1.0)),
        )
        for i, s in enumerate(pool)
    ]
    survivors = [i for i, d in enumerate(adjusted) if d > elapsed]
    tails = [
        (s.tail_seconds, weights[i])
        for i, s in enumerate(pool)
        if s.tail_seconds is not None
        and s.finished_at is not None
        and s.finished_at < known_at
    ]

    def tail_quantile(q: float) -> float | None:
        return weighted_quantile(tails, q) if tails else None

    overdue = not survivors
    partial = not tails
    grid = None
    if overdue:
        typical = weighted_quantile(zip(adjusted, weights, strict=True), 0.5)
        floor = max(OVERDUE_MIN_SECONDS, OVERDUE_FRACTION * typical)
        p50 = floor + (tail_quantile(0.5) or 0.0)
        p80 = floor + (tail_quantile(0.8) or 0.0)
    else:
        remaining = [(adjusted[i] - elapsed, weights[i]) for i in survivors]
        joint = [
            (adjusted[i] - elapsed + t, weights[i])
            for i in survivors
            if (t := pool[i].tail_seconds) is not None
            and pool[i].finished_at is not None
            and pool[i].finished_at < known_at  # type: ignore[operator]
        ]
        grid = None
        if len(joint) >= MIN_JOINT:
            p50 = weighted_quantile(joint, 0.5)
            p80 = weighted_quantile(joint, 0.8)
            partial = False
            grid = tuple(weighted_quantile(joint, q) for q in LEVELS)
        else:
            p50 = weighted_quantile(remaining, 0.5) + (tail_quantile(0.5) or 0.0)
            p80 = weighted_quantile(remaining, 0.8) + (tail_quantile(0.8) or 0.0)

    return Estimate(
        stage=subject.stage,
        p50_seconds=p50,
        p80_seconds=max(p80, p50),
        level=level,
        n=len(pool),
        overdue=overdue,
        partial=partial,
        elapsed_seconds=elapsed,
        reviewer=subject.reviewer,
        explanation=_explain(subject, level, len(pool), elapsed, overdue, hours),
        grid=grid,
        factors=tuple(info),
        similar=_similar(pool, weights, survivors, subject, dims),
    )


def _similar(
    pool: Sequence[StageSample],
    weights: Sequence[float],
    survivors: Sequence[int],
    subject: Subject,
    dims: Sequence[str],
    count: int = 5,
) -> tuple[StageSample, ...]:
    """The reference class: the surviving samples sharing most groups with the
    subject, then the heaviest (most recent)."""
    ranked = sorted(
        survivors,
        key=lambda i: (
            -sum(
                1
                for d in dims
                if subject.value(d) and pool[i].value(d) == subject.value(d)
            ),
            -weights[i],
        ),
    )
    return tuple(pool[i] for i in ranked[:count])


def _explain(
    subject: Subject,
    level: str,
    n: int,
    elapsed: float,
    overdue: bool,
    hours: WorkingHours,
) -> str:
    phrase = _PHRASES.get(subject.stage, subject.stage.replace("_", " ")).format(
        reviewer=subject.reviewer or "the reviewer"
    )
    noun = _NOUNS.get(subject.stage, "similar stages")
    basis = f"based on {n} {noun}"
    text = f"{phrase}, {format_working(elapsed, hours)} in, {basis}"
    if overdue:
        text += "; already longer than all of them"
    return text


# ---- the stages after a parent --------------------------------------------------------


def post_parent_tail(
    history: History, params: Params, known_at: datetime
) -> tuple[float, float] | None:
    """``(p50, p80)`` working seconds a child still needs once its parent
    landed: what followed a "blocked" stage, else how long "merge" took;
    ``None`` with no sample."""
    for stage in ("blocked", "merge"):
        samples = history.before(stage, known_at, STAGE_CAP)
        weights = _weights(samples, known_at, params.half_life_weeks)
        if stage == "blocked":
            pairs = [
                (s.tail_seconds, w)
                for s, w in zip(samples, weights, strict=True)
                if s.tail_seconds is not None
                and s.finished_at is not None
                and s.finished_at < known_at
            ]
        else:
            pairs = [(s.seconds, w) for s, w in zip(samples, weights, strict=True)]
        if len(pairs) >= params.min_samples:
            return weighted_quantile(pairs, 0.5), weighted_quantile(pairs, 0.8)
    return None
