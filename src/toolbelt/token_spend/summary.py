"""What the spend views show: windows of spend, then a breakdown by model and
by task over one window. Built from the calls ``ingest`` stored, so it is as
complete as the transcripts on this machine (other machines and claude.ai are
not in it), priced at the price table's list prices.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from toolbelt.git.stack.store import ModelUsage, StackStore, TaskUsage, TokenCounts


@dataclass(frozen=True)
class WindowUsage:
    label: str
    start: datetime
    calls: int
    tokens: TokenCounts
    #: Cost of the priced calls only.
    cost_usd: float
    unpriced_calls: int


@dataclass(frozen=True)
class NamedTaskUsage:
    """``TaskUsage`` with the task named; ``repo`` and ``branch`` are None for
    the spend no claim covers, and for a task that is no longer known."""

    task_id: str | None
    repo: str | None
    branch: str | None
    calls: float
    tokens: float
    cost_usd: float
    unpriced_calls: float


@dataclass(frozen=True)
class AutonomousWindow:
    label: str
    start: datetime
    runs: int
    #: The SDK's own cost of each run, summed.
    cost_usd: float
    tokens: TokenCounts


@dataclass(frozen=True)
class AutonomousTrigger:
    trigger: str
    runs: int
    cost_usd: float


@dataclass(frozen=True)
class AutonomousTask:
    task_id: str
    repo: str | None
    branch: str
    runs: int
    cost_usd: float


@dataclass(frozen=True)
class AutonomousUsage:
    """What autonomous agent runs cost, exactly: each run records the cost the
    SDK reported. Kept apart from the interactive figures, which are estimated
    from transcripts."""

    windows: list[AutonomousWindow]
    by_trigger: list[AutonomousTrigger]
    by_task: list[AutonomousTask]


#: The autonomous run triggers, in the order the chart stacks them.
TRIGGERS = ("resolve_conflicts", "resolve_comments", "fix_ci")


@dataclass(frozen=True)
class SpendBucket:
    """What one stretch of time cost: interactive sessions, and autonomous runs
    (each run's own cost, at the time it started) by what they were for."""

    start: datetime
    interactive_usd: float
    by_trigger: dict[str, float]


@dataclass(frozen=True)
class SpendOverTime:
    #: How long each bucket is: an hour for a one-day window, else a day (UTC).
    bucket_hours: int
    buckets: list[SpendBucket]


@dataclass(frozen=True)
class SpendSummary:
    as_of: datetime
    windows: list[WindowUsage]
    #: The window the two breakdowns below cover.
    breakdown_days: int
    by_model: list[ModelUsage]
    by_task: list[NamedTaskUsage]
    #: Models whose calls have no price yet, and how many calls.
    unpriced_models: dict[str, int]
    #: When claims began to be recorded: spend before it can only be
    #: unclaimed. None when none has been recorded.
    claims_since: datetime | None
    autonomous: AutonomousUsage
    over_time: SpendOverTime


def _bucket_start(moment: datetime, bucket_hours: int) -> datetime:
    utc = moment.astimezone(UTC)
    floored = utc.replace(minute=0, second=0, microsecond=0)
    return floored if bucket_hours == 1 else floored.replace(hour=0)


def _over_time(
    store: StackStore, start: datetime, end: datetime, bucket_hours: int
) -> SpendOverTime:
    first = _bucket_start(start, bucket_hours)
    step = timedelta(hours=bucket_hours)
    starts: list[datetime] = []
    moment = first
    while moment < end:
        starts.append(moment)
        moment += step
    interactive = {s: 0.0 for s in starts}
    autonomous: dict[datetime, dict[str, float]] = {s: {} for s in starts}
    for hour, cost in store.interactive_cost_by_hour(first, end).items():
        at = datetime.fromisoformat(f"{hour}:00:00+00:00")
        interactive[_bucket_start(at, bucket_hours)] += cost
    for run in store.agent_runs(since=first):
        bucket = _bucket_start(run.started_at, bucket_hours)
        if bucket in autonomous:
            costs = autonomous[bucket]
            costs[run.trigger] = costs.get(run.trigger, 0.0) + run.cost_usd
    return SpendOverTime(
        bucket_hours=bucket_hours,
        buckets=[SpendBucket(s, interactive[s], autonomous[s]) for s in starts],
    )


def _window(
    store: StackStore, label: str, start: datetime, end: datetime
) -> WindowUsage:
    models = store.usage_by_model(start, end)
    return WindowUsage(
        label=label,
        start=start,
        calls=sum(m.calls for m in models),
        tokens=TokenCounts(
            input=sum(m.tokens.input for m in models),
            output=sum(m.tokens.output for m in models),
            cache_read=sum(m.tokens.cache_read for m in models),
            cache_write_5m=sum(m.tokens.cache_write_5m for m in models),
            cache_write_1h=sum(m.tokens.cache_write_1h for m in models),
        ),
        cost_usd=sum(m.cost_usd for m in models),
        unpriced_calls=sum(m.unpriced_calls for m in models),
    )


def _task_names(store: StackStore) -> dict[str, tuple[str, str]]:
    """``task_id`` -> (repo name, branch) for every task the store knows,
    tracked now or since removed."""
    repos = {repo.id: repo.name for repo in store.repos()}
    names = {
        facts.task_id: (repos.get(facts.repo_id, "?"), facts.name)
        for facts in store.tracked_branch_facts()
    }
    for removed in store.removed_branches():
        names.setdefault(
            removed.task_id, (repos.get(removed.repo_id, "?"), removed.name)
        )
    return names


def _named(store: StackStore, tasks: list[TaskUsage]) -> list[NamedTaskUsage]:
    names = _task_names(store)
    return [
        NamedTaskUsage(
            task_id=t.task_id,
            repo=names[t.task_id][0] if t.task_id in names else None,
            branch=names[t.task_id][1] if t.task_id in names else None,
            calls=t.calls,
            tokens=t.tokens,
            cost_usd=t.cost_usd,
            unpriced_calls=t.unpriced_calls,
        )
        for t in tasks
    ]


def _autonomous(
    store: StackStore, windows: list[tuple[str, datetime]], breakdown_start: datetime
) -> AutonomousUsage:
    runs = store.agent_runs(since=min(start for _, start in windows))
    names = _task_names(store)

    def window(label: str, start: datetime) -> AutonomousWindow:
        inside = [run for run in runs if run.started_at >= start]
        return AutonomousWindow(
            label=label,
            start=start,
            runs=len(inside),
            cost_usd=sum(run.cost_usd for run in inside),
            tokens=TokenCounts(
                input=sum(run.tokens.input for run in inside),
                output=sum(run.tokens.output for run in inside),
                cache_read=sum(run.tokens.cache_read for run in inside),
                cache_write_5m=sum(run.tokens.cache_write_5m for run in inside),
                cache_write_1h=sum(run.tokens.cache_write_1h for run in inside),
            ),
        )

    chosen = [run for run in runs if run.started_at >= breakdown_start]
    triggers: dict[str, list[float]] = {}
    tasks: dict[str, tuple[str, list[float]]] = {}
    for run in chosen:
        triggers.setdefault(run.trigger, []).append(run.cost_usd)
        tasks.setdefault(run.task_id, (run.branch, []))[1].append(run.cost_usd)
    return AutonomousUsage(
        windows=[window(label, start) for label, start in windows],
        by_trigger=sorted(
            (
                AutonomousTrigger(trigger, len(costs), sum(costs))
                for trigger, costs in triggers.items()
            ),
            key=lambda t: -t.cost_usd,
        ),
        by_task=sorted(
            (
                AutonomousTask(
                    task_id=task_id,
                    repo=names[task_id][0] if task_id in names else None,
                    branch=branch,
                    runs=len(costs),
                    cost_usd=sum(costs),
                )
                for task_id, (branch, costs) in tasks.items()
            ),
            key=lambda t: -t.cost_usd,
        ),
    )


def summarize(
    store: StackStore, now: datetime | None = None, *, breakdown_days: int = 7
) -> SpendSummary:
    now = now or datetime.now(UTC)
    end = now + timedelta(seconds=1)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    starts = [
        ("Last 24 hours", now - timedelta(hours=24)),
        ("Last 7 days", now - timedelta(days=7)),
        ("Last 30 days", now - timedelta(days=30)),
        ("This month (UTC)", month_start),
    ]
    windows = [_window(store, label, start, end) for label, start in starts]
    start = now - timedelta(days=breakdown_days)
    return SpendSummary(
        as_of=now,
        windows=windows,
        breakdown_days=breakdown_days,
        by_model=store.usage_by_model(start, end),
        by_task=_named(store, store.usage_by_task(start, end)),
        unpriced_models=store.unpriced_models(),
        claims_since=store.first_claim_seen(),
        autonomous=_autonomous(store, starts, start),
        over_time=_over_time(
            store, start, end, bucket_hours=1 if breakdown_days == 1 else 24
        ),
    )
