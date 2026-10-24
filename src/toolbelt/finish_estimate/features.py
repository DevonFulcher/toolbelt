"""The feature registry: every input a forecast may use, declared once.

A feature has a name, an encoding (``categorical``, ``numeric`` or ``log``: how
a model reads it), the stages it applies to, and how it is computed from facts
known **strictly before** an instant (``compute``). The snapshot dataset
(``snapshots``), the baseline's factor dimensions (``STAGE_DIMS``), the
challenger models' inputs and the ablation all read it. A feature that is a
column of ``task_stage_durations`` names its ``StageSample`` attribute
(``sample_attr``); the others are derived from the earlier history.

Adding a feature is one ``Feature`` here (and a column only when it must be
stored). ``STAGE_DIMS`` has an entry for every kind a stage can be (a test
fails for one without), either its dimensions or the explicit ``DEFAULT_DIMS``.
"""

import statistics
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, get_args

from toolbelt.finish_estimate.working_hours import WorkingHours

if TYPE_CHECKING:
    from toolbelt.finish_estimate.snapshots import Span, TaskTimeline

type Value = str | float | None
type Encoding = Literal["categorical", "numeric", "log"]

#: A reviewer's trailing latency (and a repo's CI wait) is the median of the
#: last this many that ended before the instant.
TRAILING = 20

_REVIEW_STAGES = frozenset({"wait_review", "address_review"})
_CI_STAGES = frozenset({"wait_ci", "fix_ci", "merge", "blocked"})


@dataclass(frozen=True)
class Feature:
    name: str
    encoding: Encoding
    #: ``None``: applies to every stage.
    stages: frozenset[str] | None
    #: Computed from ``context`` as known strictly before ``before``.
    compute: Callable[[Context, TaskTimeline, Span, datetime], Value]
    #: The ``StageSample`` attribute holding it, for a stored column.
    sample_attr: str | None = None
    doc: str = ""

    def applies(self, stage: str) -> bool:
        return self.stages is None or stage in self.stages


class Context:
    """The earlier history the derived features are computed from."""

    def __init__(self, tasks: Sequence[TaskTimeline], hours: WorkingHours) -> None:
        self.hours = hours
        self.reviews: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        self.requests: dict[str, list[tuple[str, datetime, datetime | None]]] = (
            defaultdict(list)
        )
        self.ci: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        #: Spans waiting on the user: (task_key, start, end|None).
        self.mine: list[tuple[str, datetime, datetime | None]] = []
        self.slack: dict[str, list[datetime]] = {}
        for task in tasks:
            self.slack[task.task_key] = sorted(task.slack_posts)
            for span in task.spans:
                if span.who == "you":
                    self.mine.append((task.task_key, span.started_at, span.ended_at))
                if span.stage == "wait_review" and span.reviewer:
                    self.requests[span.reviewer].append(
                        (task.task_key, span.started_at, span.ended_at)
                    )
                    if span.ended_at is not None:
                        self.reviews[span.reviewer].append(
                            (
                                span.ended_at,
                                hours.seconds(span.started_at, span.ended_at),
                            )
                        )
                if span.stage == "wait_ci" and span.ended_at is not None:
                    self.ci[task.repo].append(
                        (span.ended_at, hours.seconds(span.started_at, span.ended_at))
                    )
        for table in (self.reviews, self.ci):
            for items in table.values():
                items.sort()
        self._ends = {
            id(table): {k: [t for t, _ in v] for k, v in table.items()}
            for table in (self.reviews, self.ci)
        }

    def trailing(
        self, table: dict[str, list[tuple[datetime, float]]], key: str, before: datetime
    ) -> float | None:
        if key not in table:
            return None
        end = bisect_left(self._ends[id(table)][key], before)
        recent = table[key][max(0, end - TRAILING) : end]
        return statistics.median(v for _, v in recent) if recent else None


def _open(items, own: str, before: datetime) -> int:
    return sum(
        1
        for key, start, end in items
        if key != own and start < before and (end is None or end >= before)
    )


def _slack_hours(
    ctx: Context, task: TaskTimeline, span: Span, before: datetime
) -> Value:
    posts = ctx.slack.get(task.task_key, [])
    index = bisect_left(posts, before)
    return (before - posts[index - 1]).total_seconds() / 3600 if index else None


REGISTRY: tuple[Feature, ...] = (
    Feature(
        "reviewer",
        "categorical",
        _REVIEW_STAGES,
        lambda c, t, s, b: s.reviewer,
        sample_attr="reviewer",
        doc="Who the stage waits on (or whose review is addressed).",
    ),
    Feature(
        "round",
        "categorical",
        _REVIEW_STAGES,
        lambda c, t, s, b: str(s.round),
        sample_attr="round",
        doc="How many times the task has been in this stage.",
    ),
    Feature(
        "size",
        "categorical",
        None,
        lambda c, t, s, b: str(t.size_bucket),
        sample_attr="size_bucket",
        doc="log2 of lines added and deleted.",
    ),
    Feature(
        "size_log2",
        "numeric",
        None,
        lambda c, t, s, b: float(t.size_bucket),
        doc="The size bucket as a number, for the learned models.",
    ),
    Feature(
        "repo",
        "categorical",
        None,
        lambda c, t, s, b: t.repo,
        sample_attr="repo",
        doc="The repo.",
    ),
    Feature(
        "hour_of_week",
        "numeric",
        None,
        lambda c, t, s, b: float(c.hours.hour_of_week(b)),
        doc="Hours since Monday 00:00 in the working-hours zone.",
    ),
    Feature(
        "reviewer_latency",
        "log",
        _REVIEW_STAGES,
        lambda c, t, s, b: c.trailing(c.reviews, s.reviewer, b) if s.reviewer else None,
        doc="The reviewer's median latency over their last reviews.",
    ),
    Feature(
        "reviewer_open_requests",
        "numeric",
        _REVIEW_STAGES,
        lambda c, t, s, b: (
            float(_open(c.requests.get(s.reviewer, ()), t.task_key, b))
            if s.reviewer
            else None
        ),
        doc="Review requests of theirs on other tasks open then.",
    ),
    Feature(
        "repo_ci_seconds",
        "log",
        _CI_STAGES,
        lambda c, t, s, b: c.trailing(c.ci, t.repo, b),
        doc="The repo's typical CI wait.",
    ),
    Feature(
        "queue_length",
        "numeric",
        None,
        lambda c, t, s, b: float(_open(c.mine, t.task_key, b)),
        doc="Other tasks waiting on the user then.",
    ),
    Feature(
        "slack_hours_since",
        "log",
        None,
        _slack_hours,
        doc="Hours since the task's last Slack post or bump.",
    ),
)
BY_NAME = {f.name: f for f in REGISTRY}

#: The categorical features of ``StageSample`` a baseline factor may use.
DEFAULT_DIMS: tuple[str, ...] = ("repo",)

#: The baseline's factor dimensions for each kind a stage can be, most specific
#: first. Every kind is listed; one that is deliberately like the rest says
#: ``DEFAULT_DIMS``.
STAGE_DIMS: dict[str, tuple[str, ...]] = {
    "open_pr": ("size",),
    "personal_review": ("size",),
    "done": DEFAULT_DIMS,
    "resolve_conflicts": ("size",),
    "address_review": ("reviewer", "size"),
    "resolve_comments": ("size",),
    "fix_ci": ("repo", "size"),
    "wait_ci": ("repo",),
    "publish": ("size",),
    "blocked": ("repo",),
    "merge": ("repo",),
    "wait_review": ("reviewer", "round"),
    "request_review": ("size",),
    "post_slack": DEFAULT_DIMS,
    "bump_slack": DEFAULT_DIMS,
    "claimed": DEFAULT_DIMS,
    "autonomous": DEFAULT_DIMS,
    "deciding": DEFAULT_DIMS,
}


def model_features(stage: str) -> list[Feature]:
    """The numeric and log features that apply to ``stage`` (what a learned
    model reads besides the stage and the elapsed time)."""
    return [f for f in REGISTRY if f.encoding != "categorical" and f.applies(stage)]


def stage_kinds() -> tuple[str, ...]:
    from toolbelt.ui.next_action import NextKind

    return get_args(NextKind.__value__)
