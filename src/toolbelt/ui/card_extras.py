"""What a task card shows beyond its PR status: the autonomous agents' view of
the branch (what an agent would do, why none is on it, its latest run) and
what the task has cost. Read from the dispatcher's last decisions, the run
records and the spend ledger when the page asks."""

from datetime import UTC, datetime, timedelta

from toolbelt.autonomous_agents.dispatcher import Dispatcher
from toolbelt.git.stack.store import AgentRun, StackStore
from toolbelt.ui import api_models as api
from toolbelt.ui.agents_view import reason_info, run_info
from toolbelt.ui.snapshot import row_id

#: Spend is totalled from the start of the ledger.
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def build_card_extras(
    dispatcher: Dispatcher,
    store: StackStore,
    now: datetime,
    spend_read_at: datetime | None = None,
) -> api.CardExtras:
    repo_names = {repo.id: repo.name for repo in store.repos()}
    runs_by_branch: dict[tuple[int, str], list[AgentRun]] = {}
    for run in store.agent_runs():
        runs_by_branch.setdefault((run.repo_id, run.branch), []).append(run)
    interactive = {
        usage.task_id: usage
        for usage in store.usage_by_task(_EPOCH, now + timedelta(seconds=1))
        if usage.task_id is not None
    }
    branches = []
    for facts in store.tracked_branch_facts():
        key = (facts.repo_id, facts.name)
        runs = runs_by_branch.get(key, [])
        decision = dispatcher.decisions.get(key)
        usage = interactive.get(facts.task_id)
        branches.append(
            api.BranchExtras(
                id=row_id(*key),
                agent=api.CardAgent(
                    has_action=decision is not None,
                    trigger=decision.trigger if decision else None,  # type: ignore[arg-type]
                    reason=reason_info(decision.reason)
                    if decision and decision.reason
                    else None,
                    can_override=decision.can_override if decision else False,
                    latest_run=run_info(runs[0], repo_names) if runs else None,
                    runs=len(runs),
                ),
                spend=api.CardSpend(
                    interactive_usd=usage.cost_usd if usage else 0.0,
                    interactive_unpriced_calls=usage.unpriced_calls if usage else 0.0,
                    autonomous_usd=sum(run.cost_usd for run in runs),
                    autonomous_runs=len(runs),
                ),
            )
        )
    return api.CardExtras(
        branches=branches,
        agents_decided_at=dispatcher.ticked_at.isoformat()
        if dispatcher.ticked_at
        else None,
        spend_read_at=spend_read_at.isoformat() if spend_read_at else None,
    )
