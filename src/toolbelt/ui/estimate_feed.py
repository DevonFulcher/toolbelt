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
    BASELINE,
    Ablation,
    ablate,
    choose_champions,
    metrics_by_stage,
    metrics_of,
    regime_changes,
    replay,
)
from toolbelt.finish_estimate.baseline import BaselineModel
from toolbelt.finish_estimate.challengers import CHALLENGERS
from toolbelt.finish_estimate.features import REGISTRY, Context
from toolbelt.finish_estimate.forecast import (
    Forecast,
    ForecastModel,
    TrainingData,
    full_grid,
    grid_add,
    grid_max,
    grid_zero,
    read_p50_p80,
)
from toolbelt.finish_estimate.model import (
    DEFAULT_PARAMS,
    MODEL_VERSION,
    Params,
    Prior,
    StageSample,
    size_bucket,
)
from toolbelt.finish_estimate.snapshots import (
    SnapshotFeatures,
    Span,
    TaskTimeline,
    build_snapshots,
    features_at,
    samples_of_timelines,
)
from toolbelt.finish_estimate.working_hours import WorkingHours
from toolbelt.git.stack.store import (
    BacktestRow,
    ModelFitRow,
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


# ---- timelines ---------------------------------------------------------------------


def timelines_of(
    lives: Sequence[tuple[aggregate.Task, list[aggregate.Segment]]],
) -> list[TaskTimeline]:
    """The tasks with a PR as timelines, the stage still going (of an open
    PR) included: the snapshot dataset's input, with open tasks censored."""
    out = []
    for task, segments in lives:
        if task.pr is None:
            continue
        facts = task.pr.facts
        stages = _coalesce(segments)
        spans = []
        seen: dict[str, int] = defaultdict(int)
        for index, segment in enumerate(stages):
            if segment.state in _NOT_A_STAGE:
                continue
            seen[segment.state] += 1
            going = facts.state == "OPEN" and index == len(stages) - 1
            spans.append(
                Span(
                    stage=segment.state,
                    started_at=segment.start,
                    ended_at=None if going else segment.end,
                    reviewer=reviewer_of(facts.items, segment.start, facts.author),
                    round=seen[segment.state],
                    who=segment.who,
                )
            )
        merged = facts.state == "MERGED"
        out.append(
            TaskTimeline(
                task_key=task_key(task.pr.repo, facts.number),
                repo=task.pr.repo,
                size_bucket=size_bucket(facts.additions, facts.deletions),
                spans=tuple(spans),
                outcome="merged"
                if merged
                else "closed"
                if facts.state == "CLOSED"
                else "open",
                ended_at=facts.merged_at if merged else facts.closed_at,
                slack_posts=tuple(
                    sorted(
                        datetime.fromisoformat(event.t)
                        for lane in task.lanes
                        if lane.lane == "slack"
                        for event in lane.events
                    )
                ),
            )
        )
    return out


def timelines_from_rows(rows: Sequence[StageDurationRow]) -> list[TaskTimeline]:
    """The stored (ended) stages as timelines: the context live features are
    computed from. Their outcome is not read."""
    grouped: dict[str, list[StageDurationRow]] = defaultdict(list)
    for row in rows:
        grouped[row.task_key].append(row)
    out = []
    for key, items in grouped.items():
        items.sort(key=lambda r: r.started_at)
        out.append(
            TaskTimeline(
                task_key=key,
                repo=items[0].repo,
                size_bucket=items[0].size_bucket,
                spans=tuple(
                    Span(r.stage, r.started_at, r.ended_at, r.reviewer, r.round, r.kind)
                    for r in items
                ),
                outcome="merged" if items[0].finished_at else "open",
                ended_at=items[0].finished_at,
            )
        )
    return out


# ---- the models, fitted -------------------------------------------------------------

#: A challenger is refitted when its fit is this old, or this many stage rows
#: have been stored since.
REFIT_AFTER = timedelta(days=1)
REFIT_NEW_ROWS = 25
#: The replay refits each model at most this often (the job's cost).
JOB_REFIT = timedelta(days=2)
ABLATION_REFIT = timedelta(days=7)
JOB_MAX_TASKS = 100


class ModelHub:
    """The fitted challengers, shared by the background job (which fits them)
    and the card computation (which predicts with them). Fits are stored, so a
    restart restores them instead of refitting at once."""

    def __init__(self, names: Sequence[str] = tuple(CHALLENGERS)) -> None:
        self.names = tuple(names)
        self.models: dict[str, ForecastModel] = {}
        self.fits: dict[str, ModelFitRow] = {}
        self._restored = False

    def restore(self, store: StackStore) -> None:
        if self._restored:
            return
        self._restored = True
        for row in store.model_fits():
            if row.model not in self.names:
                continue
            model = CHALLENGERS[row.model]()
            if row.model_version != model.version:
                continue
            model.load(row.state)
            self.models[row.model] = model
            self.fits[row.model] = row

    def due(self, store: StackStore, now: datetime) -> list[str]:
        self.restore(store)
        rows = len(store.stage_durations())
        return [
            name
            for name in self.names
            if name not in self.fits
            or now - self.fits[name].fitted_at >= REFIT_AFTER
            or rows >= self.fits[name].stage_rows + REFIT_NEW_ROWS
        ]

    def fit(
        self,
        store: StackStore,
        timelines: Sequence[TaskTimeline],
        hours: WorkingHours,
        now: datetime,
        names: Sequence[str] | None = None,
    ) -> list[str]:
        """Fits the due models (or ``names``) on everything known now and
        stores them."""
        todo = list(names if names is not None else self.due(store, now))
        if not todo:
            return []
        data = TrainingData(
            now,
            hours,
            samples_of_timelines(timelines, hours),
            build_snapshots(timelines, hours, now),
        )
        rows = len(store.stage_durations())
        for name in todo:
            model = CHALLENGERS[name]()
            model.fit(data)
            state = model.dump()
            self.models[name] = model
            if state is not None:
                fit = ModelFitRow(
                    name, now, model.trained_on, rows, model.version, state
                )
                store.save_model_fit(fit)
                self.fits[name] = fit
        return todo


# ---- the backtest ------------------------------------------------------------------


def _json_metrics(m) -> dict:
    return {
        "stage": m.stage,
        "n": m.n,
        "unknown": m.unknown,
        "censored": m.censored,
        "coverage": m.coverage,
        "coverage_adjusted": m.coverage_adjusted,
        "median_abs_error_seconds": m.median_abs_error_seconds,
        "median_width_seconds": m.median_width_seconds,
        "loss": m.loss,
    }


def _fresh_models(
    hours: WorkingHours,
    params: Params,
    challengers: Sequence[str],
    priors: dict[str, Prior],
) -> dict[str, ForecastModel]:
    return {BASELINE: BaselineModel(hours, params, priors)} | {
        name: CHALLENGERS[name]() for name in challengers
    }


def run_backtest_and_store(
    store: StackStore,
    hours: WorkingHours,
    now: datetime,
    timelines: Sequence[TaskTimeline],
    *,
    challengers: Sequence[str] = tuple(CHALLENGERS),
    ablation: str | None = "regression",
    priors: dict[str, Prior] | None = None,
) -> bool:
    """Replays the history through the baseline and the challengers on the
    same rows, picks the champion per stage, ablates the candidate features of
    one model, and stores it all with the time. Returns whether it stored
    anything (nothing finished to replay: not worth a row)."""
    priors = priors or {}
    params = stored_params(store)
    outcomes = replay(
        _fresh_models(hours, params, challengers, priors),
        timelines,
        hours,
        now,
        max_tasks=JOB_MAX_TASKS,
        refit_every=JOB_REFIT,
    )
    if not outcomes[BASELINE]:
        return False
    champions = choose_champions(outcomes)
    measured = [o for rows in outcomes.values() for o in rows if not o.censored]
    if not measured:
        return False
    ablated: list[Ablation] = []
    if ablation is not None and ablation in CHALLENGERS:
        names = [f.name for f in REGISTRY if f.encoding != "categorical"]
        ablated = ablate(
            lambda excluded: CHALLENGERS[ablation](excluded),  # type: ignore[call-arg]
            names,
            timelines,
            hours,
            now,
            max_tasks=JOB_MAX_TASKS,
            refit_every=ABLATION_REFIT,
        )
    base = outcomes[BASELINE]
    overall = metrics_of(base)
    metrics = {
        "loss": overall.loss,
        "overall": _json_metrics(overall),
        "by_stage": [_json_metrics(m) for m in metrics_by_stage(base)],
        "regime": [
            r.__dict__
            for r in regime_changes(samples_of_timelines(timelines, hours), now)
        ],
        "models": {
            name: {
                "overall": _json_metrics(metrics_of(rows)),
                "by_stage": [_json_metrics(m) for m in metrics_by_stage(rows)],
            }
            for name, rows in outcomes.items()
        },
        "champions": {
            stage: {
                "model": c.model,
                "margin": c.margin,
                "points": c.points,
                "evidence": [e.__dict__ for e in c.evidence],
            }
            for stage, c in champions.items()
        },
        "ablation_model": ablation if ablated else "",
        "ablation": [a.__dict__ for a in ablated],
    }
    store.save_backtest(
        BacktestRow(
            ran_at=now,
            model_version=MODEL_VERSION,
            params=json.dumps(
                {
                    "half_life_weeks": params.half_life_weeks,
                    "min_samples": params.min_samples,
                    "shrinkage": params.shrinkage,
                }
            ),
            metrics=json.dumps(metrics),
        )
    )
    return True


class EstimateJob:
    """Keeps the stage history up to date, refits the learned models when due
    and runs the backtest daily (in a worker thread).

    ``lives`` produces the tasks and their Next (from the cached GitHub
    history, no network); ``changed`` is told when something new was stored."""

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
        hub: ModelHub | None = None,
        priors: Callable[[], dict[str, Prior]] = dict,
        challengers: Sequence[str] = tuple(CHALLENGERS),
        ablation: str | None = "regression",
    ) -> None:
        self._db_path = db_path
        self._lives = lives
        self._hours = hours
        self._wall_clock = wall_clock
        self._changed = changed
        self.hub = hub or ModelHub(challengers)
        self._priors = priors
        self._challengers = tuple(challengers)
        self._ablation = ablation
        #: The cached GitHub history is not loaded yet (the first fetch is
        #: still going), so there was nothing to rebuild from.
        self.waiting_for_history = True

    def _work(self, lives, force: bool) -> bool:
        store = StackStore(self._db_path())
        try:
            now = self._wall_clock()
            hours = self._hours()
            refresh_history(store, lives, now)
            timelines = timelines_of(lives)
            ran = False
            if force or backtest_due(store, now):
                ran = run_backtest_and_store(
                    store,
                    hours,
                    now,
                    timelines,
                    challengers=self._challengers,
                    ablation=self._ablation,
                    priors=self._priors(),
                )
            self.hub.fit(
                store,
                timelines,
                hours,
                now,
                names=None if not force else self.hub.names,
            )
            return ran
        finally:
            store.close()

    async def run_once(self, *, force_backtest: bool = False) -> bool:
        """Rebuilds the history, refits what is due and backtests when due (or
        forced). Returns whether a backtest was stored."""
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
    #: When its PR was posted or bumped in Slack.
    slack_posts: tuple[datetime, ...] = ()
    #: The card's branch row id, for the breakdown.
    row_id: str = ""


def _current(task: LiveTask, now: datetime) -> aggregate.Segment | None:
    """The stage a task is in: its open PR's last segment, still going."""
    if task.facts.state != "OPEN" or not task.segments:
        return None
    last = task.segments[-1]
    if last.end < now or last.state in _NOT_A_STAGE:
        return None
    return last


def live_timeline(task: LiveTask, now: datetime) -> tuple[TaskTimeline, Span] | None:
    """The task as a timeline whose last span is the stage it is in."""
    current = _current(task, now)
    if current is None:
        return None
    (timeline,) = timelines_of(
        [(aggregate.Task(pr=_record(task), started=None), list(task.segments))]
    )
    spans = list(timeline.spans)
    return (
        TaskTimeline(
            timeline.task_key,
            timeline.repo,
            timeline.size_bucket,
            tuple(spans),
            "open",
            None,
            task.slack_posts,
        ),
        spans[-1],
    )


def _record(task: LiveTask) -> aggregate.PrRecord:
    return aggregate.PrRecord(
        repo=task.repo,
        head_branch=task.branch,
        facts=task.facts,
        review_now="none",
    )


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


@dataclass(frozen=True)
class _Finish:
    """A model's finish for a task, after the parent it is stacked on."""

    forecast: Forecast
    p50_seconds: float | None
    p80_seconds: float | None
    grid: tuple[float, ...] | None
    #: The parent has no answer, so this is at least the task's own.
    at_least: bool = False
    parent_key: str | None = None
    parent_p50: float | None = None
    parent_p80: float | None = None


def _latest_metrics(store: StackStore) -> dict:
    runs = store.backtests(limit=1)
    return json.loads(runs[-1].metrics) if runs else {}


def champion_for(metrics: dict, stage: str) -> tuple[str, str]:
    """The model that answers for ``stage`` and the reason, from the latest
    backtest's champions (the stage's, else the overall one, else the
    baseline)."""
    champions = metrics.get("champions") or {}
    for key in (stage, "all"):
        entry = champions.get(key)
        if entry and entry["model"] != BASELINE:
            return entry["model"], (
                f"champion of {key.replace('_', ' ')}: {entry['margin']:.0%} lower pinball "
                f"loss than the baseline on {entry['points']} test points"
            )
        if entry and key == stage:
            return BASELINE, (
                f"the baseline stays for {key.replace('_', ' ')}: no model beat it by the "
                f"margin on at least the minimum test points ({entry['points']} here)"
            )
    return BASELINE, "the baseline answers: no backtest has chosen another model yet"


def live_estimates(
    store: StackStore,
    tasks: Sequence[LiveTask],
    hours: WorkingHours,
    now: datetime,
    hub: ModelHub | None = None,
    priors: dict[str, Prior] | None = None,
) -> tuple[dict[str, api.CardEstimate], dict[str, api.EstimateBreakdown]]:
    """When each task in flight will be finished, by task id: the champion's
    answer for the card, and every model's for the popup. A task stacked on a
    parent that has not merged finishes no sooner than its parent does: per
    model, the later of the two (the product of their distributions' CDFs on
    the quantile grid, the parent's already including its own ancestors) plus
    what usually follows a parent landing (a convolution). Each stage entered
    is recorded once as a prediction (the champion's)."""
    params = stored_params(store)
    rows = store.stage_durations()
    samples = samples_of(rows, hours)
    baseline = BaselineModel(hours, params, priors or {})
    baseline.fit(TrainingData(now, hours, samples, []))
    models: dict[str, ForecastModel] = {BASELINE: baseline}
    if hub is not None:
        hub.restore(store)
        models |= hub.models
    metrics = _latest_metrics(store)
    post = baseline.post_parent_grid()

    live = {t.task_id: lt for t in tasks if (lt := live_timeline(t, now)) is not None}
    context = Context(
        [
            *(
                tl
                for tl in timelines_from_rows(rows)
                if tl.task_key not in {l[0].task_key for l in live.values()}
            ),
            *(tl for tl, _ in live.values()),
        ],
        hours,
    )
    by_branch = {(t.repo, t.branch): t for t in tasks}
    states: dict[str, SnapshotFeatures] = {
        tid: features_at(context, tl, span, now) for tid, (tl, span) in live.items()
    }
    forecasts: dict[tuple[str, str], Forecast] = {
        (tid, name): model.predict(state)
        for tid, state in states.items()
        for name, model in models.items()
    }
    memo: dict[tuple[str, str], _Finish] = {}

    def finish(task: LiveTask, name: str, depth: int = 0) -> _Finish | None:
        if task.task_id not in live:
            return None
        key = (task.task_id, name)
        if key in memo:
            return memo[key]
        own = forecasts[key]
        stage = states[task.task_id].stage
        own_grid = grid_zero() if stage == "blocked" else full_grid(own)
        parent = by_branch.get((task.repo, task.facts.base_ref))
        above = (
            finish(parent, name, depth + 1)
            if parent is not None and parent is not task and depth < 20
            else None
        )
        if above is None:
            result = _Finish(own, own.p50_seconds, own.p80_seconds, own_grid)
            if stage == "blocked" and own_grid is not None:
                p50, p80 = read_p50_p80(own_grid)
                result = _Finish(own, p50, p80, own_grid)
        elif above.grid is None or above.p50_seconds is None:
            # The parent has no estimate: at least the task's own, no number
            # made up for the wait.
            result = _Finish(
                own,
                own.p50_seconds,
                own.p80_seconds,
                own_grid,
                at_least=True,
                parent_key=task_key(parent.repo, parent.facts.number)
                if parent
                else None,
            )
        elif own_grid is None:
            result = _Finish(
                own,
                None,
                None,
                None,
                parent_key=task_key(parent.repo, parent.facts.number)
                if parent
                else None,
            )
        else:
            combined = grid_add(grid_max(own_grid, above.grid), post or grid_zero())
            p50, p80 = read_p50_p80(combined)
            result = _Finish(
                own,
                p50,
                p80,
                combined,
                parent_key=task_key(parent.repo, parent.facts.number)
                if parent
                else None,
                parent_p50=above.p50_seconds,
                parent_p80=above.p80_seconds,
            )
        memo[key] = result
        return result

    cards: dict[str, api.CardEstimate] = {}
    breakdowns: dict[str, api.EstimateBreakdown] = {}
    for task in tasks:
        if task.task_id not in live:
            continue
        state = states[task.task_id]
        chosen, reason = champion_for(metrics, state.stage)
        results = {name: finish(task, name) for name in models}
        if results.get(chosen) is None or results[chosen].p50_seconds is None:  # type: ignore[union-attr]
            chosen, reason = (
                BASELINE,
                reason + "; it had no answer here, so the baseline answers",
            )
        champ = results[chosen]
        assert champ is not None
        cards[task.task_id] = _card_of(champ, chosen, state.stage, hours, now)
        breakdowns[task.task_id] = _breakdown(
            task,
            state,
            results,
            chosen,
            reason,
            metrics,
            hub,
            hours,
            now,
            forecasts[(task.task_id, BASELINE)],
        )
        if champ.p50_seconds is not None and champ.p80_seconds is not None:
            store.record_prediction(
                PredictionRow(
                    task_key=task_key(task.repo, task.facts.number),
                    stage=state.stage,
                    stage_started_at=state.stage_started_at,
                    made_at=now,
                    p50_at=hours.advance(now, champ.p50_seconds),
                    p80_at=hours.advance(now, champ.p80_seconds),
                    level=champ.forecast.level,
                    n=champ.forecast.n,
                    model_version=MODEL_VERSION,
                    model=chosen,
                )
            )
    return cards, breakdowns


def _text(f: _Finish) -> str:
    text = f.forecast.text
    if f.at_least:
        text += "; at least this: its parent has no estimate"
    elif f.parent_key:
        text += f"; not before {f.parent_key} lands"
    return text


def _card_of(
    champ: _Finish, model: str, stage: str, hours: WorkingHours, now: datetime
) -> api.CardEstimate:
    f = champ.forecast
    known = champ.p50_seconds is not None and champ.p80_seconds is not None
    return api.CardEstimate(
        known=known,
        stage=stage,
        finish_p50=_iso(hours.advance(now, champ.p50_seconds))
        if known and champ.p50_seconds is not None
        else None,
        finish_p80=_iso(hours.advance(now, champ.p80_seconds))
        if known and champ.p80_seconds is not None
        else None,
        overdue=bool(f.details.get("overdue")),
        partial=bool(f.details.get("partial")),
        level=f.level,
        n=f.n,
        explanation=_text(champ),
        model=model,
    )


def _breakdown(
    task: LiveTask,
    state: SnapshotFeatures,
    results: dict[str, _Finish | None],
    chosen: str,
    reason: str,
    metrics: dict,
    hub: ModelHub | None,
    hours: WorkingHours,
    now: datetime,
    base: Forecast,
) -> api.EstimateBreakdown:
    stage_metrics = {
        name: next(
            (m for m in data["by_stage"] if m["stage"] == state.stage),
            data["overall"],
        )
        for name, data in (metrics.get("models") or {}).items()
    }
    base_loss = (stage_metrics.get(BASELINE) or {}).get("loss")
    accuracy_rows = []
    for name, entry in stage_metrics.items():
        fit = hub.fits.get(name) if hub else None
        accuracy_rows.append(
            api.ModelAccuracy(
                model=name,
                points=entry["n"] - entry["censored"],
                coverage=entry["coverage"],
                loss=entry["loss"],
                median_abs_error_seconds=entry["median_abs_error_seconds"],
                trained_on=fit.trained_on if fit else None,
                fitted_at=_iso(fit.fitted_at) if fit else None,
                loss_vs_baseline=(
                    entry["loss"] / base_loss
                    if entry["loss"] is not None and base_loss
                    else None
                ),
            )
        )
    forecasts = []
    for name, f in results.items():
        if f is None:
            continue
        known = f.p50_seconds is not None and f.p80_seconds is not None
        forecasts.append(
            api.ModelForecast(
                model=name,
                champion=name == chosen,
                known=known,
                finish_p50=_iso(hours.advance(now, f.p50_seconds))
                if known and f.p50_seconds is not None
                else None,
                finish_p80=_iso(hours.advance(now, f.p80_seconds))
                if known and f.p80_seconds is not None
                else None,
                level=f.forecast.level,
                n=f.forecast.n,
                explanation=_text(f),
                details=dict(f.forecast.details),
            )
        )
    details = base.details
    champ = results[chosen]
    parent = None
    if champ is not None and champ.parent_key:
        parent = api.ParentWait(
            task_key=champ.parent_key,
            parent_known=champ.parent_p50 is not None,
            parent_p50=_iso(hours.advance(now, champ.parent_p50))
            if champ.parent_p50 is not None
            else None,
            parent_p80=_iso(hours.advance(now, champ.parent_p80))
            if champ.parent_p80 is not None
            else None,
            combined_p50=_iso(hours.advance(now, champ.p50_seconds))
            if champ.p50_seconds is not None
            else None,
            combined_p80=_iso(hours.advance(now, champ.p80_seconds))
            if champ.p80_seconds is not None
            else None,
        )
    return api.EstimateBreakdown(
        id=task.row_id,
        stage=state.stage,
        champion=chosen,
        champion_reason=reason,
        models=forecasts,
        accuracy=accuracy_rows,
        path=[
            api.PathStage(
                stage=p["stage"],
                share=p["share"],
                p50_seconds=p["p50_seconds"],
                p80_seconds=p["p80_seconds"],
            )
            for p in details.get("path", [])
        ],
        parent=parent,
        factors=[api.EstimateFactor(**f) for f in details.get("factors", [])],
        similar=[
            api.SimilarTask(
                task_key=s["task_key"],
                stage=s["stage"],
                reviewer=s["reviewer"],
                seconds=s["seconds"],
                tail_seconds=s["tail_seconds"],
                ended_at=s["ended_at"],
            )
            for s in details.get("similar", [])
        ],
    )


# ---- the track record ---------------------------------------------------------------


def _stage_accuracy(data: dict) -> api.EstimateStageAccuracy:
    return api.EstimateStageAccuracy(
        stage=data["stage"],
        n=data["n"],
        unknown=data["unknown"],
        coverage=data["coverage"],
        median_abs_error_seconds=data["median_abs_error_seconds"],
        median_width_seconds=data["median_width_seconds"],
        censored=data.get("censored", 0),
        coverage_adjusted=data.get("coverage_adjusted"),
        loss=data.get("loss"),
    )


def _backtest_of(row: BacktestRow) -> api.EstimateBacktest:
    metrics = json.loads(row.metrics)
    return api.EstimateBacktest(
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
        models=[
            api.ModelMetrics(
                model=name,
                overall=_stage_accuracy(data["overall"]),
                by_stage=[_stage_accuracy(i) for i in data["by_stage"]],
            )
            for name, data in (metrics.get("models") or {}).items()
        ],
        champions=[
            api.EstimateChampion(
                stage=stage,
                model=c["model"],
                margin=c["margin"],
                points=c["points"],
                evidence=[api.ChampionEvidence(**e) for e in c["evidence"]],
            )
            for stage, c in (metrics.get("champions") or {}).items()
        ],
        ablation_model=metrics.get("ablation_model", ""),
        ablation=[api.EstimateAblation(**a) for a in metrics.get("ablation") or []],
    )


def accuracy(store: StackStore, hours: WorkingHours) -> api.EstimateAccuracy:
    """The stored backtests, who the champion of each stage was over time, and
    the estimates shown compared with the actual finishes (a task counts once
    it merged)."""
    backtests = [_backtest_of(row) for row in store.backtests()]
    changes: list[api.ChampionChange] = []
    previous: dict[str, str] = {}
    for run in backtests:
        for c in run.champions:
            if c.stage in previous and previous[c.stage] != c.model:
                changes.append(
                    api.ChampionChange(
                        stage=c.stage,
                        previous=previous[c.stage],
                        model=c.model,
                        at=run.ran_at,
                    )
                )
            previous[c.stage] = c.model
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
        champion_changes=changes,
        fits=[
            api.ModelFitInfo(
                model=f.model,
                fitted_at=f.fitted_at.isoformat(),
                trained_on=f.trained_on,
            )
            for f in store.model_fits()
        ],
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


# ---- on demand ----------------------------------------------------------------------


async def backtest_now(db_path: Path, config_file: Path) -> list[str]:
    """Rebuilds the stage history from the stored GitHub history (no
    network), refits the models and backtests now, whether or not one is due;
    returns what to print."""
    from toolbelt.finish_estimate.config import (
        configured_priors,
        configured_working_hours,
    )
    from toolbelt.ui.aggregate_feed import AggregateService, read_history

    async def no_search(query: str, cursor: str | None) -> dict:
        raise RuntimeError("the backtest does not use the network")

    async def no_remote(path: Path) -> str | None:
        return None

    hours = configured_working_hours(config_file)
    priors = configured_priors(config_file)
    service = AggregateService(
        search=no_search,
        history=read_history,
        remote_of=no_remote,
        finished=lambda *_: False,
        wall_clock=utc_now,
        db_path=lambda: db_path,
    )
    job = EstimateJob(
        db_path=lambda: db_path,
        lives=service.lives,
        hours=lambda: hours,
        wall_clock=utc_now,
        priors=lambda: priors,
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
    day = track.working_day_seconds
    lines = [f"{rows} stage rows stored."]
    for m in latest.models:
        o = m.overall
        lines.append(
            f"{m.model}: {o.n - o.unknown - o.censored} scored predictions ({o.unknown} unknown, "
            f"{o.censored} for open tasks), coverage "
            + (f"{o.coverage:.0%}" if o.coverage is not None else "n/a")
            + ", loss "
            + (f"{o.loss / 3600:.2f}h" if o.loss is not None else "n/a")
            + (
                f", median error {o.median_abs_error_seconds / day:.2f} working days"
                if o.median_abs_error_seconds is not None
                else ""
            )
        )
    for c in latest.champions:
        lines.append(
            f"champion for {c.stage}: {c.model} (margin {c.margin:.0%}, {c.points} points)"
        )
    for a in latest.ablation:
        if a.delta is not None:
            lines.append(
                f"ablation {a.feature}: {a.delta:+.0f}s pinball loss when removed ({a.points} points)"
            )
    for change in latest.regime:
        lines.append(
            f"{change.stage} durations moved {change.change:+.0%} over {change.weeks} weeks."
        )
    return lines
