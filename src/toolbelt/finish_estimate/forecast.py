"""The interface every forecasting model implements, and the arithmetic on
finish distributions (carried on the fixed 19-quantile grid ``LEVELS``) that
stacked tasks need.

A model is fitted on ``TrainingData`` (only what was known strictly before
``known_at``) and answers ``predict(state)`` with a ``Forecast``: the working
time left to the task's finish at p50 and p80, optionally the whole grid, and a
JSON-able ``details`` payload explaining it. Adding a model is a class with
``name``, ``version``, ``fit`` and ``predict`` registered in
``toolbelt.finish_estimate.registry``; the backtest harness runs it on the same
splits as every other model.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import NormalDist
from typing import Any, Protocol

from toolbelt.finish_estimate.model import LEVELS, StageSample
from toolbelt.finish_estimate.snapshots import SnapshotFeatures, SnapshotRow
from toolbelt.finish_estimate.working_hours import WorkingHours

P50 = LEVELS.index(0.5)
P80 = LEVELS.index(0.8)


@dataclass(frozen=True)
class Forecast:
    """``None`` quantiles: the model has no answer (it never invents one)."""

    model: str
    p50_seconds: float | None
    p80_seconds: float | None
    #: The model's own fallback label ("stage", "reviewer+size", ...).
    level: str = ""
    #: The training rows or samples this answer rests on.
    n: int = 0
    text: str = ""
    #: The remaining time at ``LEVELS``, when the model has a distribution.
    grid: tuple[float, ...] | None = None
    #: What the model learned that matters for this answer (JSON-able).
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.p50_seconds is not None and self.p80_seconds is not None


@dataclass(frozen=True)
class TrainingData:
    """What a model may learn from at ``known_at``: ended stages (their finish
    known only if it came before ``known_at``) and snapshot rows whose
    targets are censored at ``known_at`` where they were not known yet."""

    known_at: datetime
    hours: WorkingHours
    samples: Sequence[StageSample]
    rows: Sequence[SnapshotRow]


class ForecastModel(Protocol):
    name: str
    #: Bumped when the model changes what it predicts for the same data.
    version: int
    #: How many rows or samples the last ``fit`` used.
    trained_on: int

    def fit(self, data: TrainingData) -> None: ...

    def predict(self, state: SnapshotFeatures) -> Forecast: ...

    def dump(self) -> bytes | None:
        """The fitted state to store, so a restart does not refit
        (``None``: nothing to keep)."""
        ...

    def load(self, state: bytes) -> None: ...


# ---- distributions on the grid ---------------------------------------------------------


def full_grid(forecast: Forecast) -> tuple[float, ...] | None:
    """The forecast's distribution on ``LEVELS``: its own, else the lognormal
    through its p50 and p80."""
    if forecast.grid is not None:
        return forecast.grid
    if forecast.p50_seconds is None or forecast.p80_seconds is None:
        return None
    normal = NormalDist()
    p50 = max(forecast.p50_seconds, 1.0)
    p80 = max(forecast.p80_seconds, p50)
    sigma = (math.log(p80) - math.log(p50)) / normal.inv_cdf(0.8)
    return tuple(math.exp(math.log(p50) + sigma * normal.inv_cdf(q)) for q in LEVELS)


def grid_max(a: Sequence[float], b: Sequence[float]) -> tuple[float, ...]:
    """The distribution of ``max(A, B)`` for independent ``A`` and ``B``: the
    product of their CDFs. Positive correlation (a slow week slows both)
    would make the true maximum earlier than this, so it errs late."""
    values = sorted({*a, *b})
    cdf = [
        (sum(x <= v for x in a) / len(a)) * (sum(x <= v for x in b) / len(b))
        for v in values
    ]
    out = []
    for q in LEVELS:
        out.append(next(v for v, c in zip(values, cdf, strict=True) if c >= q - 1e-9))
    return tuple(out)


def grid_add(a: Sequence[float], b: Sequence[float]) -> tuple[float, ...]:
    """The distribution of ``A + B`` for independent ``A`` and ``B``, by
    convolving their grids."""
    sums = sorted(x + y for x in a for y in b)
    return tuple(sums[max(0, math.ceil(q * len(sums)) - 1)] for q in LEVELS)


def grid_zero() -> tuple[float, ...]:
    return tuple(0.0 for _ in LEVELS)


def read_p50_p80(grid: Sequence[float]) -> tuple[float, float]:
    return grid[P50], max(grid[P80], grid[P50])
