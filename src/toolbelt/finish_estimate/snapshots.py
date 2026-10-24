"""The snapshot dataset: one row per (task, stage, checkpoint instant), with
the features known strictly before that instant and the targets (the working
time left to the end of the stage and to the task's finish).

A task still open at the end of the data is **right-censored**: its targets are
the working time observed so far, a lower bound, and the row says so
(``*_censored``). Such rows are kept (dropping them would bias estimates
short); a model that cannot use censoring may still see the flag. A task closed
without merging has a stage target but no finish target.

Checkpoints are the start of a stage and each full working day spent in it
(``MAX_CHECKPOINTS`` at most), so the time already waited is a feature the
dataset exercises. Features computed from other tasks (a reviewer's trailing
latency and open requests, a repo's typical CI duration) use only spans that
ended before the checkpoint; a span still going is known to be going, and its
end is not read.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from toolbelt.finish_estimate.features import REGISTRY, Context, Value
from toolbelt.finish_estimate.model import StageSample
from toolbelt.finish_estimate.working_hours import WorkingHours

#: At most this many checkpoints in one stage (its start, then each full
#: working day).
MAX_CHECKPOINTS = 10

type Outcome = Literal["merged", "open", "closed"]


@dataclass(frozen=True)
class Span:
    """A stage of a task; ``ended_at`` is ``None`` while it is still going at
    the end of the data."""

    stage: str
    started_at: datetime
    ended_at: datetime | None
    reviewer: str = ""
    round: int = 1
    #: Who the stage waited on (``you``, ``others``, ``ci``, ...).
    who: str = ""


@dataclass(frozen=True)
class TaskTimeline:
    """A task's stages in order. ``ended_at`` is when it merged (``merged``) or
    was closed (``closed``); ``None`` while ``open``."""

    task_key: str
    repo: str
    size_bucket: int
    spans: tuple[Span, ...]
    outcome: Outcome
    ended_at: datetime | None = None
    #: When the task's PR was posted or bumped in Slack.
    slack_posts: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class SnapshotFeatures:
    """What a model is asked about: everything known strictly before
    ``checkpoint``."""

    task_key: str
    stage: str
    checkpoint: datetime
    stage_started_at: datetime
    #: Working seconds spent in the stage so far.
    elapsed_seconds: float
    #: Every registry feature (``features.REGISTRY``) by name, computed from
    #: what was known strictly before ``checkpoint``; ``None``: not known.
    values: Mapping[str, Value] = field(default_factory=dict)

    def value(self, name: str) -> Value:
        return self.values.get(name)


@dataclass(frozen=True)
class SnapshotRow:
    """Features and targets. ``stage_ended_at`` / ``task_ended_at`` are the
    observed ends (``None``: not ended); the ``*_remaining_seconds`` targets
    are measured from the checkpoint to that end, or to ``observed_until`` when
    it is not known yet (then ``*_censored``)."""

    features: SnapshotFeatures
    outcome: Outcome
    stage_ended_at: datetime | None
    task_ended_at: datetime | None
    #: The end of the data the targets were observed up to.
    observed_until: datetime
    stage_remaining_seconds: float
    stage_censored: bool
    #: ``None`` for a closed task: it never finished.
    finish_remaining_seconds: float | None
    finish_censored: bool

    @property
    def checkpoint(self) -> datetime:
        return self.features.checkpoint


def _targets(
    features: SnapshotFeatures,
    outcome: Outcome,
    stage_ended: datetime | None,
    task_ended: datetime | None,
    until: datetime,
    hours: WorkingHours,
) -> tuple[float, bool, float | None, bool]:
    cp = features.checkpoint
    known_stage = stage_ended is not None and stage_ended <= until
    stage_remaining = hours.seconds(cp, stage_ended if known_stage else until)  # type: ignore[arg-type]
    merged_known = (
        outcome == "merged" and task_ended is not None and task_ended <= until
    )
    if outcome == "closed":
        finish: float | None = None
        censored = False
    elif merged_known:
        finish = hours.seconds(cp, task_ended)  # type: ignore[arg-type]
        censored = False
    else:
        finish = hours.seconds(cp, until)
        censored = True
    return stage_remaining, not known_stage, finish, censored


def with_known_at(
    row: SnapshotRow, known_at: datetime, hours: WorkingHours
) -> SnapshotRow:
    """The row as it was known at ``known_at`` (after its checkpoint): an end
    after ``known_at`` is not known, so that target is censored at it."""
    if (
        row.observed_until <= known_at
        and (
            row.stage_censored
            or row.stage_ended_at is None
            or row.stage_ended_at <= known_at
        )
        and (row.task_ended_at is None or row.task_ended_at <= known_at)
    ):
        return row
    stage_ended = (
        row.stage_ended_at
        if row.stage_ended_at and row.stage_ended_at <= known_at
        else None
    )
    if row.outcome == "closed":
        task_ended = (
            row.task_ended_at
            if row.task_ended_at and row.task_ended_at <= known_at
            else None
        )
        outcome: Outcome = "closed" if task_ended else "open"
    else:
        task_ended = (
            row.task_ended_at
            if row.task_ended_at and row.task_ended_at <= known_at
            else None
        )
        outcome = "merged" if task_ended else "open"
    stage_remaining, stage_censored, finish, censored = _targets(
        row.features, outcome, stage_ended, task_ended, known_at, hours
    )
    return replace(
        row,
        outcome=outcome,
        stage_ended_at=stage_ended,
        task_ended_at=task_ended,
        observed_until=known_at,
        stage_remaining_seconds=stage_remaining,
        stage_censored=stage_censored,
        finish_remaining_seconds=finish,
        finish_censored=censored,
    )


def samples_of_timelines(
    tasks: Sequence[TaskTimeline], hours: WorkingHours
) -> list[StageSample]:
    """The ended stages of the tasks as the baseline's samples."""
    out = []
    for task in tasks:
        finished = task.ended_at if task.outcome == "merged" else None
        for span in task.spans:
            if span.ended_at is None:
                continue
            out.append(
                StageSample(
                    task_key=task.task_key,
                    stage=span.stage,
                    started_at=span.started_at,
                    ended_at=span.ended_at,
                    seconds=hours.seconds(span.started_at, span.ended_at),
                    reviewer=span.reviewer,
                    repo=task.repo,
                    size_bucket=task.size_bucket,
                    round=span.round,
                    finished_at=finished,
                    tail_seconds=(
                        hours.seconds(span.ended_at, finished) if finished else None
                    ),
                )
            )
    return out


def features_at(
    context: Context, task: TaskTimeline, span: Span, checkpoint: datetime
) -> SnapshotFeatures:
    """The features of ``span`` at ``checkpoint``, from what ``context``
    knew strictly before it."""
    hours = context.hours
    return SnapshotFeatures(
        task_key=task.task_key,
        stage=span.stage,
        checkpoint=checkpoint,
        stage_started_at=span.started_at,
        elapsed_seconds=hours.seconds(span.started_at, checkpoint),
        values={
            f.name: f.compute(context, task, span, checkpoint)
            for f in REGISTRY
            if f.applies(span.stage)
        },
    )


def build_snapshots(
    tasks: Sequence[TaskTimeline], hours: WorkingHours, data_end: datetime
) -> list[SnapshotRow]:
    """Every checkpoint of every stage of the tasks, oldest first, with
    targets observed up to ``data_end``."""
    context = Context(tasks, hours)
    rows: list[SnapshotRow] = []
    for task in tasks:
        for span in task.spans:
            stage_end = (
                span.ended_at if span.ended_at and span.ended_at <= data_end else None
            )
            limit = stage_end or data_end
            for index in range(MAX_CHECKPOINTS):
                checkpoint = (
                    span.started_at
                    if index == 0
                    else hours.advance(span.started_at, index * hours.day_seconds)
                )
                if checkpoint >= limit and index > 0:
                    break
                if checkpoint > data_end:
                    break
                features = features_at(context, task, span, checkpoint)
                stage_remaining, stage_cens, finish, finish_cens = _targets(
                    features, task.outcome, stage_end, task.ended_at, data_end, hours
                )
                rows.append(
                    SnapshotRow(
                        features=features,
                        outcome=task.outcome,
                        stage_ended_at=stage_end,
                        task_ended_at=task.ended_at
                        if task.ended_at and task.ended_at <= data_end
                        else None,
                        observed_until=data_end,
                        stage_remaining_seconds=stage_remaining,
                        stage_censored=stage_cens,
                        finish_remaining_seconds=finish,
                        finish_censored=finish_cens,
                    )
                )
    rows.sort(key=lambda r: (r.checkpoint, r.features.task_key, r.features.stage))
    return rows
