"""The Agents view's data: what the dispatcher decided, the runs so far, and
the limits they run under, as the API's wire models."""

from datetime import datetime, timedelta

from toolbelt.autonomous_agents.dispatcher import Dispatcher
from toolbelt.autonomous_agents.eligibility import (
    Reason,
    month_start,
    spend_since,
)
from toolbelt.git.stack.store import AgentRun, StackStore
from toolbelt.ui import api_models as api
from toolbelt.ui.snapshot import row_id

RECENT_RUNS = 50


def reason_info(reason: Reason) -> api.AgentReason:
    return api.AgentReason(
        code=reason.code,
        text=reason.text,
        until=reason.until.isoformat() if reason.until else None,
    )


def run_info(run: AgentRun, repo_names: dict[int, str]) -> api.AgentRunInfo:
    return api.AgentRunInfo(
        run_id=run.run_id,
        id=row_id(run.repo_id, run.branch),
        repo=repo_names.get(run.repo_id, "?"),
        branch=run.branch,
        trigger=run.trigger,  # type: ignore[arg-type]
        model=run.model,
        state=run.state,  # type: ignore[arg-type]
        started_at=run.started_at.isoformat(),
        ended_at=run.ended_at.isoformat() if run.ended_at else None,
        turns=run.turns,
        cost_usd=run.cost_usd,
        summary=run.summary,
        error=run.error,
        activity=run.activity,
        result=run.result,  # type: ignore[arg-type]
    )


def runs_window_start(now: datetime) -> datetime:
    """How far back the dispatcher needs runs: the month, or 24 hours if
    that reaches further."""
    return min(month_start(now), now - timedelta(hours=24))


def build_agents(
    dispatcher: Dispatcher,
    store: StackStore,
    *,
    enabled: bool,
    launching: bool,
    now: datetime,
) -> api.Agents:
    limits = dispatcher.limits
    screen = dispatcher.screen
    runs = store.agent_runs(since=runs_window_start(now))
    repo_names = {repo.id: repo.name for repo in store.repos()}
    decisions = sorted(
        dispatcher.decisions.values(),
        key=lambda d: (d.reason is not None, d.repo, d.branch),
    )
    return api.Agents(
        enabled=enabled,
        launching=launching,
        dry_run=dispatcher.dry_run,
        ticked_at=dispatcher.ticked_at.isoformat() if dispatcher.ticked_at else None,
        error=dispatcher.last_error,
        blockers=[reason_info(b) for b in dispatcher.blockers],
        limits=api.AgentLimitsInfo(
            model=limits.model,
            run_usd=limits.run_usd,
            run_turns=limits.run_turns,
            run_timeout_seconds=limits.run_timeout_seconds,
            task_runs_per_head=limits.task_runs_per_head,
            task_runs_per_day=limits.task_runs_per_day,
            task_usd_per_day=limits.task_usd_per_day,
            global_usd_per_day=limits.global_usd_per_day,
            global_usd_per_month=limits.global_usd_per_month,
            active_working_days=limits.active_working_days,
        ),
        spend=api.AgentSpend(
            last_24_hours_usd=spend_since(runs, now - timedelta(hours=24)),
            this_month_usd=spend_since(runs, month_start(now)),
        ),
        screen=api.AgentScreen(awake=screen.awake, locked=screen.locked)
        if screen
        else None,
        signed_in=dispatcher.signed_in,
        decisions=[
            api.AgentDecision(
                id=row_id(d.repo_id, d.branch),
                repo=d.repo,
                branch=d.branch,
                trigger=d.trigger,  # type: ignore[arg-type]
                reason=reason_info(d.reason) if d.reason else None,
                decided_at=d.decided_at.isoformat(),
                can_override=d.can_override,
                starting=(d.repo_id, d.branch) in dispatcher.starting,
                start_error=dispatcher.start_errors.get((d.repo_id, d.branch)),
            )
            for d in decisions
        ],
        runs=[run_info(run, repo_names) for run in runs[:RECENT_RUNS]],
    )
