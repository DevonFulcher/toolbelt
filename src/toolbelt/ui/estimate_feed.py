"""The finish estimate in the UI: the history it is made from (the stages of
the tasks, from the aggregate's replay of every task's Next), the daily
backtest that tunes it, and the estimate each card and the aggregate view
show. The arithmetic is in ``toolbelt.finish_estimate``; this reads and
writes the stack DB. See ``docs/finish-estimate.md``."""

import asyncio
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from toolbelt.finish_estimate.backtest import (
    BacktestResult,
    run_backtest,
)
from toolbelt.finish_estimate.model import (
    DEFAULT_PARAMS,
    MODEL_VERSION,
    Estimate,
    History,
    Params,
    StageSample,
    Subject,
    estimate,
    post_parent_tail,
    size_bucket,
)
from toolbelt.finish_estimate.working_hours import WorkingHours
from toolbelt.git.stack.store import (
    BacktestRow,
    PredictionRow,
    StackStore,
    StageDurationRow,
)
from toolbelt.logger import logger
from toolbelt.ui import aggregate
from toolbelt.ui import api_models as api
from toolbelt.ui.pr_activity import PrFacts, reviewer_of

#: How often the stage history is rebuilt from the cached PRs, and how long
#: after a backtest the next one is due.
REFRESH_SECONDS = 900.0
#: How soon to try again while the GitHub history is still being fetched.
RETRY_SECONDS = 10.0
BACKTEST_EVERY = timedelta(days=1)

#: A segment that is not a stage of the work.
_NOT_A_STAGE = {"done"}


def task_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


# ---- the history -------------------------------------------------------------------


def _coalesce(segments: Sequence[aggregate.Segment]) -> list[aggregate.Segment]:
    """Adjacent segments of the same stage as one (splitting at another
    task's change does not end a stage)."""
    out: list[aggregate.Segment] = []
    for segment in segments:
        if (
            out
            and out[-1].end == segment.start
            and (out[-1].state, out[-1].who) == (segment.state, segment.who)
        ):
            out[-1] = aggregate.Segment(
                out[-1].start, segment.end, segment.state, segment.who
            )
        else:
            out.append(segment)
    return out


def stage_rows(
    lives: Sequence[tuple[aggregate.Task, list[aggregate.Segment]]], now: datetime
) -> list[StageDurationRow]:
    """Every stage that ended, of the tasks with a PR. A stage is a segment of
    the task's Next (``aggregate.task_segments``); the one still going is not
    a row yet. A task that merged has ``finished_at``; one closed unmerged
    has stages but no finish."""
    rows: list[StageDurationRow] = []
    for task, segments in lives:
        if task.pr is None:
            continue
        facts = task.pr.facts
        key = task_key(task.pr.repo, facts.number)
        finished = facts.merged_at if facts.state == "MERGED" else None
        seen: dict[str, int] = defaultdict(int)
        stages = _coalesce(segments)
        for index, segment in enumerate(stages):
            if segment.state in _NOT_A_STAGE:
                continue
            # The last stage of a PR that is open is still going.
            if facts.state == "OPEN" and index == len(stages) - 1:
                continue
            seen[segment.state] += 1
            reviewer = reviewer_of(facts.items, segment.start, facts.author)
            rows.append(
                StageDurationRow(
                    task_key=key,
                    task_id=None,
                    stage=segment.state,
                    kind=segment.who,
                    started_at=segment.start,
                    ended_at=segment.end,
                    actor=_actor(segment, reviewer, facts),
                    reviewer=reviewer,
                    repo=task.pr.repo,
                    additions=facts.additions,
                    deletions=facts.deletions,
                    size_bucket=size_bucket(facts.additions, facts.deletions),
                    round=seen[segment.state],
                    finished_at=finished,
                )
            )
    return rows


def _actor(segment: aggregate.Segment, reviewer: str, facts: PrFacts) -> str:
    if segment.who == "you":
        return facts.author
    if segment.who == "others":
        return reviewer
    return segment.who


def samples_of(
    rows: Sequence[StageDurationRow], hours: WorkingHours
) -> list[StageSample]:
    """The rows as the estimator's samples, with the lengths in working time."""
    return [
        StageSample(
            task_key=row.task_key,
            stage=row.stage,
            started_at=row.started_at,
            ended_at=row.ended_at,
            seconds=hours.seconds(row.started_at, row.ended_at),
            reviewer=row.reviewer,
            repo=row.repo,
            size_bucket=row.size_bucket,
            round=row.round,
            finished_at=row.finished_at,
            tail_seconds=(
                hours.seconds(row.ended_at, row.finished_at)
                if row.finished_at is not None
                else None
            ),
        )
        for row in rows
    ]


def stored_params(store: StackStore) -> Params:
    """The hyperparameters of the latest backtest of this model version."""
    for row in reversed(store.backtests(limit=20)):
        if row.model_version == MODEL_VERSION:
            return Params(**json.loads(row.params))
    return DEFAULT_PARAMS


def refresh_history(
    store: StackStore,
    lives: Sequence[tuple[aggregate.Task, list[aggregate.Segment]]],
    now: datetime,
) -> int:
    """Rebuilds the stage rows of every task in ``lives``; returns how many
    rows there are."""
    rows = stage_rows(lives, now)
    store.replace_stage_durations(rows)
    return len(rows)


def backtest_due(store: StackStore, now: datetime) -> bool:
    runs = store.backtests(limit=1)
    return not runs or now - runs[-1].ran_at >= BACKTEST_EVERY


def run_backtest_and_store(
    store: StackStore, hours: WorkingHours, now: datetime
) -> BacktestResult:
    """Replays the stored history, tunes the hyperparameters and stores the
    result with the time (when it predicted anything); the live estimates use
    the new parameters."""
    samples = samples_of(store.stage_durations(), hours)
    result = run_backtest(samples, hours, now)
    if result.overall.n == 0:
        return result  # nothing finished to replay: not worth a row
    metrics = result.to_dict()
    for key in ("params", "grid"):
        metrics.pop(key)
    if metrics["loss"] in (float("inf"), float("-inf")):
        metrics["loss"] = None
    store.save_backtest(
        BacktestRow(
            ran_at=now,
            model_version=MODEL_VERSION,
            params=json.dumps(
                {
                    "half_life_weeks": result.params.half_life_weeks,
                    "min_samples": result.params.min_samples,
                    "shrinkage": result.params.shrinkage,
                }
            ),
            metrics=json.dumps(metrics),
        )
    )
    return result


class EstimateJob:
    """Keeps the stage history up to date and runs the backtest daily.

    ``lives`` produces the tasks and their Next (from the cached GitHub
    history, no network); ``changed`` is told when a new history or
    parameters were stored."""

    def __init__(
        self,
        *,
        db_path: Callable[[], Path],
        lives: Callable[
            [], Awaitable[list[tuple[aggregate.Task, list[aggregate.Segment]]]]
        ],
        hours: Callable[[], WorkingHours],
        wall_clock: Callable[[], datetime],
        changed: Callable[[], None] = lambda: None,
    ) -> None:
        self._db_path = db_path
        self._lives = lives
        self._hours = hours
        self._wall_clock = wall_clock
        self._changed = changed
        #: The cached GitHub history is not loaded yet (the first fetch is
        #: still going), so there was nothing to rebuild from.
        self.waiting_for_history = True

    def _work(self, lives, force: bool) -> bool:
        store = StackStore(self._db_path())
        try:
            now = self._wall_clock()
            refresh_history(store, lives, now)
            ran = force or backtest_due(store, now)
            if ran:
                run_backtest_and_store(store, self._hours(), now)
            return ran
        finally:
            store.close()

    async def run_once(self, *, force_backtest: bool = False) -> bool:
        """Rebuilds the history and, when one is due (or forced), backtests.
        Returns whether a backtest ran."""
        lives = await self._lives()
        self.waiting_for_history = not lives
        ran = await asyncio.to_thread(self._work, lives, force_backtest)
        self._changed()
        return ran

    async def run(self, interval: float = REFRESH_SECONDS) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("Could not refresh the finish estimate")
            await asyncio.sleep(RETRY_SECONDS if self.waiting_for_history else interval)


# ---- the estimate of a live task -----------------------------------------------------


@dataclass(frozen=True)
class LiveTask:
    """A task in flight, as the estimate needs it."""

    task_id: str
    repo: str
    #: The task's head branch (a PR whose base is it is stacked on it).
    branch: str
    facts: PrFacts
    segments: Sequence[aggregate.Segment]


def _current(task: LiveTask, now: datetime) -> aggregate.Segment | None:
    """The stage a task is in: its open PR's last segment, still going."""
    if task.facts.state != "OPEN" or not task.segments:
        return None
    last = task.segments[-1]
    if last.end < now or last.state in _NOT_A_STAGE:
        return None
    return last


def _subject(task: LiveTask, segment: aggregate.Segment) -> Subject:
    # The stage began where the run of equal states did.
    started = segment.start
    for earlier in reversed(_coalesce(task.segments)):
        if earlier.end == segment.end:
            started = earlier.start
            break
    facts = task.facts
    seen = sum(
        1
        for earlier in _coalesce(task.segments)
        if earlier.state == segment.state and earlier.end <= started
    )
    return Subject(
        stage=segment.state,
        started_at=started,
        reviewer=reviewer_of(facts.items, started, facts.author),
        repo=task.repo,
        size_bucket=size_bucket(facts.additions, facts.deletions),
        round=seen + 1,
    )


def _card(
    result: Estimate, p50: datetime | None, p80: datetime | None
) -> api.CardEstimate:
    return api.CardEstimate(
        known=result.known,
        stage=result.stage,
        finish_p50=p50.isoformat() if p50 else None,
        finish_p80=p80.isoformat() if p80 else None,
        overdue=result.overdue,
        partial=result.partial,
        level=result.level,
        n=result.n,
        explanation=result.explanation,
    )


def live_estimates(
    store: StackStore,
    tasks: Sequence[LiveTask],
    hours: WorkingHours,
    now: datetime,
) -> dict[str, api.CardEstimate]:
    """When each task in flight will be finished, by task id, from the stored
    history and the latest backtest's parameters. A task stacked on a parent
    that has not merged finishes no sooner than its parent does, plus the
    steps that followed a parent landing before. Each stage entered is
    recorded once as a prediction, to be compared with the actual finish."""
    params = stored_params(store)
    history = History(samples_of(store.stage_durations(), hours))
    post = post_parent_tail(history, params, now)
    by_branch = {(t.repo, t.branch): t for t in tasks}
    memo: dict[str, tuple[Estimate, datetime | None, datetime | None]] = {}

    def finish(
        task: LiveTask, depth: int = 0
    ) -> tuple[Estimate, datetime | None, datetime | None] | None:
        if task.task_id in memo:
            return memo[task.task_id]
        segment = _current(task, now)
        if segment is None:
            return None
        subject = _subject(task, segment)
        own = estimate(history, subject, now, params, hours)
        p50 = (
            hours.advance(now, own.p50_seconds) if own.p50_seconds is not None else None
        )
        p80 = (
            hours.advance(now, own.p80_seconds) if own.p80_seconds is not None else None
        )
        parent = by_branch.get((task.repo, task.facts.base_ref))
        if parent is not None and parent is not task and depth < 20:
            above = finish(parent, depth + 1)
            if above is not None and above[1] is not None and above[2] is not None:
                lag50, lag80 = post or (0.0, 0.0)
                late50 = hours.advance(above[1], lag50)
                late80 = hours.advance(above[2], lag80)
                p50 = max(p50, late50) if p50 else late50
                p80 = max(p80, late80) if p80 else late80
        memo[task.task_id] = (own, p50, p80)
        return memo[task.task_id]

    out: dict[str, api.CardEstimate] = {}
    for task in tasks:
        found = finish(task)
        if found is None:
            continue
        own, p50, p80 = found
        out[task.task_id] = _card(own, p50, p80)
        segment = _current(task, now)
        if segment is not None and p50 is not None and p80 is not None:
            store.record_prediction(
                PredictionRow(
                    task_key=task_key(task.repo, task.facts.number),
                    stage=own.stage,
                    stage_started_at=_subject(task, segment).started_at,
                    made_at=now,
                    p50_at=p50,
                    p80_at=p80,
                    level=own.level,
                    n=own.n,
                    model_version=MODEL_VERSION,
                )
            )
    return out


# ---- the track record ---------------------------------------------------------------


def _stage_accuracy(data: dict) -> api.EstimateStageAccuracy:
    return api.EstimateStageAccuracy(
        stage=data["stage"],
        n=data["n"],
        unknown=data["unknown"],
        coverage=data["coverage"],
        median_abs_error_seconds=data["median_abs_error_seconds"],
        median_width_seconds=data["median_width_seconds"],
    )


def accuracy(store: StackStore, hours: WorkingHours) -> api.EstimateAccuracy:
    """The stored backtests, and the estimates shown compared with the
    actual finishes (a task counts once it merged)."""
    backtests = []
    for row in store.backtests():
        metrics = json.loads(row.metrics)
        backtests.append(
            api.EstimateBacktest(
                ran_at=row.ran_at.isoformat(),
                params=api.EstimateParams(**json.loads(row.params)),
                overall=_stage_accuracy(metrics["overall"]),
                by_stage=[_stage_accuracy(item) for item in metrics["by_stage"]],
                regime=[
                    api.EstimateRegime(
                        stage=item["stage"],
                        weeks=item["weeks"],
                        change=item["change"],
                        recent_median_seconds=item["recent_median_seconds"],
                        prior_median_seconds=item["prior_median_seconds"],
                        n_recent=item["n_recent"],
                        n_prior=item["n_prior"],
                    )
                    for item in metrics["regime"]
                ],
            )
        )
    finished_at = {
        row.task_key: row.finished_at
        for row in store.stage_durations()
        if row.finished_at is not None
    }
    predictions = store.predictions()
    compared = [
        (p, finished_at[p.task_key]) for p in predictions if p.task_key in finished_at
    ]
    params = stored_params(store)
    return api.EstimateAccuracy(
        working_day_seconds=hours.day_seconds,
        current=api.EstimateParams(
            half_life_weeks=params.half_life_weeks,
            min_samples=params.min_samples,
            shrinkage=params.shrinkage,
        )
        if backtests
        else None,
        backtests=backtests,
        live_made=len(predictions),
        live_finished=len(compared),
        live_coverage=(
            sum(done <= p.p80_at for p, done in compared) / len(compared)
            if compared
            else None
        ),
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


# ---- on demand ----------------------------------------------------------------------


async def backtest_now(db_path: Path, config_file: Path) -> list[str]:
    """Rebuilds the stage history from the stored GitHub history (no
    network) and backtests now, whether or not one is due; returns what to
    print."""
    from toolbelt.finish_estimate.config import configured_working_hours
    from toolbelt.ui.aggregate_feed import AggregateService, read_history

    async def no_search(query: str, cursor: str | None) -> dict:
        raise RuntimeError("the backtest does not use the network")

    async def no_remote(path: Path) -> str | None:
        return None

    hours = configured_working_hours(config_file)
    clock = utc_now
    service = AggregateService(
        search=no_search,
        history=read_history,
        remote_of=no_remote,
        finished=lambda *_: False,
        wall_clock=clock,
        db_path=lambda: db_path,
    )
    job = EstimateJob(
        db_path=lambda: db_path,
        lives=service.lives,
        hours=lambda: hours,
        wall_clock=clock,
    )
    await job.run_once(force_backtest=True)
    store = StackStore(db_path)
    try:
        track = accuracy(store, hours)
        rows = len(store.stage_durations())
    finally:
        store.close()
    if not track.backtests:
        return [f"{rows} stage rows stored; no finished work to backtest yet."]
    latest = track.backtests[-1]
    overall = latest.overall
    day = track.working_day_seconds
    lines = [
        f"{rows} stage rows stored.",
        (
            f"Chosen: half-life {latest.params.half_life_weeks} weeks, at least "
            f"{latest.params.min_samples} samples, shrinkage {latest.params.shrinkage}."
        ),
        f"{overall.n - overall.unknown} predictions ({overall.unknown} unknown): "
        + (
            f"{overall.coverage:.0%} at or under p80 (target 80%)"
            if overall.coverage is not None
            else "none known"
        )
        + (
            f", median error {overall.median_abs_error_seconds / day:.2f} working days."
            if overall.median_abs_error_seconds is not None
            else "."
        ),
    ]
    for change in latest.regime:
        lines.append(
            f"{change.stage} durations moved {change.change:+.0%} over {change.weeks} weeks."
        )
    return lines
