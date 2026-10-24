"""The baseline model: the recency-weighted empirical stage distribution
conditioned on the time already spent, with the fallback hierarchy and the
shrunk group factors of ``model.estimate``, behind the model interface."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from toolbelt.finish_estimate.forecast import Forecast, TrainingData
from toolbelt.finish_estimate.model import (
    DEFAULT_PARAMS,
    LEVELS,
    Estimate,
    History,
    Params,
    Prior,
    StageSample,
    Subject,
    estimate,
    weighted_quantile,
)
from toolbelt.finish_estimate.snapshots import SnapshotFeatures
from toolbelt.finish_estimate.working_hours import WorkingHours

#: A stage in the typical path is shown when this share of the reference
#: tasks went through it.
PATH_SHARE = 0.2


def _known(sample: StageSample, known_at: datetime) -> StageSample:
    """The sample as it was at ``known_at``: its task's finish may not have
    happened yet."""
    if sample.finished_at is not None and sample.finished_at >= known_at:
        return replace(sample, finished_at=None, tail_seconds=None)
    return sample


def _sample_json(sample: StageSample) -> dict[str, Any]:
    return {
        "task_key": sample.task_key,
        "stage": sample.stage,
        "ended_at": sample.ended_at.isoformat(),
        "seconds": sample.seconds,
        "tail_seconds": sample.tail_seconds,
        "reviewer": sample.reviewer,
    }


class BaselineModel:
    name = "baseline"
    version = 1

    def __init__(
        self,
        hours: WorkingHours,
        params: Params = DEFAULT_PARAMS,
        priors: Mapping[str, Prior] | None = None,
    ) -> None:
        self.hours = hours
        self.params = params
        self.priors = priors or {}
        self.trained_on = 0
        self._history = History([])
        self._known_at: datetime | None = None
        self._by_task: dict[str, list[StageSample]] = {}

    def fit(self, data: TrainingData) -> None:
        samples = [
            _known(s, data.known_at) for s in data.samples if s.ended_at < data.known_at
        ]
        self._history = History(samples)
        self._known_at = data.known_at
        by_task: dict[str, list[StageSample]] = defaultdict(list)
        for sample in samples:
            by_task[sample.task_key].append(sample)
        self._by_task = by_task
        self.trained_on = len(samples)

    def dump(self) -> bytes | None:
        return None

    def load(self, state: bytes) -> None:  # nothing is stored: it is refitted from rows
        pass

    def _subject(self, state: SnapshotFeatures) -> Subject:
        return Subject(state.stage, state.stage_started_at, dict(state.values))

    def estimate(self, state: SnapshotFeatures) -> Estimate:
        return estimate(
            self._history,
            self._subject(state),
            state.checkpoint,
            self.params,
            self.hours,
            known_at=self._known_at,
            prior=self.priors.get(state.stage),
        )

    def predict(self, state: SnapshotFeatures) -> Forecast:
        result = self.estimate(state)
        return Forecast(
            model=self.name,
            p50_seconds=result.p50_seconds,
            p80_seconds=result.p80_seconds,
            level=result.level,
            n=result.n,
            text=result.explanation,
            grid=result.grid,
            details={
                "level": result.level,
                "n": result.n,
                "overdue": result.overdue,
                "partial": result.partial,
                "factors": [
                    {
                        "dim": f.dim,
                        "value": f.value,
                        "n": f.n,
                        "raw": f.raw,
                        "shrunk": f.shrunk,
                    }
                    for f in result.factors
                ],
                "similar": [_sample_json(s) for s in result.similar],
                "path": self.path(result, state),
            },
        )

    def path(self, result: Estimate, state: SnapshotFeatures) -> list[dict[str, Any]]:
        """The stages that typically follow, from the tasks of the reference
        class: for each, the share of those tasks that went through it and its
        total working time per task at p50 and p80."""
        if not result.similar or self._known_at is None:
            return []
        tasks = {s.task_key: s for s in result.similar}
        durations: dict[str, list[float]] = defaultdict(list)
        order: dict[str, list[float]] = defaultdict(list)
        for key, anchor in tasks.items():
            totals: dict[str, float] = defaultdict(float)
            for later in self._by_task.get(key, []):
                if later.started_at >= anchor.ended_at:
                    totals[later.stage] += later.seconds
                    order[later.stage].append(
                        self.hours.seconds(anchor.ended_at, later.started_at)
                    )
            for stage, total in totals.items():
                durations[stage].append(total)
        rows = []
        for stage, values in durations.items():
            share = len(values) / len(tasks)
            if share < PATH_SHARE or stage == "done":
                continue
            pairs = [(v, 1.0) for v in values]
            rows.append(
                {
                    "stage": stage,
                    "share": share,
                    "p50_seconds": weighted_quantile(pairs, 0.5),
                    "p80_seconds": weighted_quantile(pairs, 0.8),
                    "starts_after_seconds": weighted_quantile(
                        [(v, 1.0) for v in order[stage]], 0.5
                    ),
                }
            )
        return sorted(rows, key=lambda r: (r["starts_after_seconds"], r["stage"]))

    def post_parent_grid(self) -> tuple[float, ...] | None:
        """What followed a parent landing (the tail of "blocked" stages, else
        how long "merge" took) on ``LEVELS``; ``None`` with too few samples."""
        known_at = self._known_at
        if known_at is None:
            return None
        blocked = [
            (s.tail_seconds, 1.0)
            for s in self._history.before("blocked", known_at, 300)
            if s.tail_seconds is not None
        ]
        merge = [(s.seconds, 1.0) for s in self._history.before("merge", known_at, 300)]
        for pairs in (blocked, merge):
            if len(pairs) >= self.params.min_samples:
                return tuple(weighted_quantile(pairs, q) for q in LEVELS)
        return None
