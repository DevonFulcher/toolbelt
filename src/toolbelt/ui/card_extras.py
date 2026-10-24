"""What a task card shows beyond its PR status: the autonomous agents' view of
the branch (what an agent would do, why none is on it, its latest run) and
what the task has cost. Read from the dispatcher's last decisions, the run
records and the spend ledger when the page asks."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from toolbelt.autonomous_agents.dispatcher import Dispatcher
from toolbelt.git.stack.store import (
    AgentRun,
    ReviewVerdict,
    StackStore,
    TrackedBranchFacts,
)
from toolbelt.ui import aggregate
from toolbelt.ui import api_models as api
from toolbelt.ui.agents_view import reason_info, run_info
from toolbelt.ui.lane_history import build_lanes
from toolbelt.ui.personal_review import VALIDATE, validated_tasks
from toolbelt.ui.pr_activity import PrFacts
from toolbelt.ui.snapshot import row_id

#: How many of a branch's runs a card lists.
RECENT_RUNS = 50

#: Spend is totalled from the start of the ledger.
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def next_lanes(
    store: StackStore,
    now: datetime,
    lanes: dict[str, list[api.TimelineLane]],
    repo_slug: Callable[[int], str | None],
    pr_facts: Callable[[int, str], tuple[PrFacts, str] | None],
) -> dict[str, tuple[api.TimelineLane, ...]]:
    """Each tracked task's lanes with its Next (the "next" lane) first: the
    same fold of ``next_action`` the aggregate view runs, on the PR's facts as
    last looked up. A task whose PR facts are not in yet has no "next" lane."""
    tracked = store.tracked_branch_facts()
    slack_from = min(
        (
            datetime.fromisoformat(event.t)
            for task_lanes in lanes.values()
            for lane in task_lanes
            if lane.lane == "slack"
            for event in lane.events
        ),
        default=None,
    )
    records: list[aggregate.PrRecord] = []
    with_pr = []
    plain: list[list[aggregate.Segment]] = []
    folded: dict[str, list[aggregate.Segment]] = {}
    for task in tracked:
        task_lanes = lanes.get(task.task_id, [])
        # The PR's facts decide whether it has a PR: the stack DB records the
        # link only when it first learns it.
        looked_up = pr_facts(task.repo_id, task.name)
        if looked_up is None:
            if task.pr is None:
                folded[task.task_id] = aggregate.task_segments(
                    None, task.started_at, None, task_lanes, slack_from, now
                )
            continue
        facts, review_now = looked_up
        record = aggregate.PrRecord(
            repo=repo_slug(task.repo_id) or f"repo:{task.repo_id}",
            head_branch=task.name,
            facts=facts,
            review_now=review_now,
            base_changes=facts.base_changes,
        )
        records.append(record)
        with_pr.append(task)
        plain.append(
            aggregate.task_segments(
                record, task.started_at, None, task_lanes, slack_from, now
            )
        )
    for task, segments in zip(
        with_pr, aggregate.with_blocked(records, plain, now), strict=True
    ):
        folded[task.task_id] = segments
    return {
        task_id: aggregate.with_next_lane(lanes.get(task_id, []), segments, now)
        for task_id, segments in folded.items()
    }


def _validation(
    facts: TrackedBranchFacts,
    runs: list[AgentRun],
    validated: set[str],
    verdicts: list[ReviewVerdict],
    *,
    has_pr: bool,
    opening: tuple[bool, str | None],
) -> api.CardValidation:
    results = [
        run
        for run in runs
        if run.task_id == facts.task_id
        and run.trigger == VALIDATE
        and run.state == "succeeded"
    ]
    latest = verdicts[-1] if verdicts else None
    return api.CardValidation(
        can_validate=not has_pr,
        validated=facts.task_id in validated,
        result=results[0].result if results else None,  # type: ignore[arg-type]
        verdict=api.ReviewVerdictInfo(
            verdict=latest.verdict,  # type: ignore[arg-type]
            note=latest.note,
            head_sha=latest.head_sha,
            at=latest.at.isoformat(),
        )
        if latest
        else None,
        opening_pr=opening[0],
        notice=opening[1],
    )


def build_card_extras(
    dispatcher: Dispatcher,
    store: StackStore,
    now: datetime,
    spend_read_at: datetime | None = None,
    repo_slug: Callable[[int], str | None] = lambda repo_id: None,
    pr_facts: Callable[[int, str], tuple[PrFacts, str] | None] = lambda *_: None,
    opening_pr: Callable[[int, str], tuple[bool, str | None]] = lambda *_: (
        False,
        None,
    ),
) -> api.CardExtras:
    """``opening_pr(repo_id, branch)``: whether the PR of an approved branch is
    being opened, and why it could not be."""
    repo_names = {repo.id: repo.name for repo in store.repos()}
    runs_by_branch: dict[tuple[int, str], list[AgentRun]] = {}
    for run in store.agent_runs():
        runs_by_branch.setdefault((run.repo_id, run.branch), []).append(run)
    interactive = {
        usage.task_id: usage
        for usage in store.usage_by_task(_EPOCH, now + timedelta(seconds=1))
        if usage.task_id is not None
    }
    lanes = build_lanes(store, now, repo_slug)
    with_next = next_lanes(store, now, lanes, repo_slug, pr_facts)
    validated = validated_tasks(store)
    verdicts: dict[str, list[ReviewVerdict]] = {}
    for verdict in store.review_verdicts():
        verdicts.setdefault(verdict.task_id, []).append(verdict)
    branches = []
    for facts in store.tracked_branch_facts():
        key = (facts.repo_id, facts.name)
        runs = runs_by_branch.get(key, [])
        decision = dispatcher.decisions.get(key)
        usage = interactive.get(facts.task_id)
        branches.append(
            api.BranchExtras(
                id=row_id(*key),
                lanes=list(with_next.get(facts.task_id, lanes.get(facts.task_id, []))),
                agent=api.CardAgent(
                    has_action=decision is not None,
                    trigger=decision.trigger if decision else None,  # type: ignore[arg-type]
                    reason=reason_info(decision.reason)
                    if decision and decision.reason
                    else None,
                    can_override=decision.can_override if decision else False,
                    starting=key in dispatcher.starting,
                    start_error=dispatcher.start_errors.get(key),
                    latest_run=run_info(runs[0], repo_names) if runs else None,
                    runs=len(runs),
                    recent_runs=[
                        run_info(run, repo_names) for run in runs[:RECENT_RUNS]
                    ],
                    validation=_validation(
                        facts,
                        runs,
                        validated,
                        verdicts.get(facts.task_id, []),
                        has_pr=facts.pr is not None or pr_facts(*key) is not None,
                        opening=opening_pr(*key),
                    ),
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
