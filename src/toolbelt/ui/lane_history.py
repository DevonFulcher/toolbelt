"""Reads each task's history for the lanes of its card's timeline
(``timeline_lanes.py``) from the stack database: observed conflict and CI
changes, agent runs, claim spans, Slack posts, the merge queue, decisions and
the task tracker moves.

Every kind a lane item gets comes from its source's ``Coverage`` (or the
source's own vocabulary), so a status value without a timeline kind is a
``KeyError`` here, not a missing item.
"""

from collections.abc import Callable
from datetime import datetime

from toolbelt.autonomous_agents.limits import VALIDATE
from toolbelt.git.stack.store import (
    AgentRun,
    RemovedBranch,
    SlackPostRow,
    StackStore,
    TrackedBranchFacts,
)
from toolbelt.ui import api_models as api
from toolbelt.ui.claims_feed import SPAN_MAX_GAP
from toolbelt.ui.personal_review import validated_since
from toolbelt.ui.status_transitions import CI, COMMENTS, CONFLICT
from toolbelt.ui.timeline_lanes import LANE_SOURCES, LANES

type TaskId = str


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _kinds(lane: str, status: str) -> dict[str, tuple[str, ...]]:
    (coverage,) = (c for c in LANES[lane].covers if c.status == status)
    return dict(coverage.kinds)


class _Lanes:
    """The items found so far, per branch and lane."""

    def __init__(self) -> None:
        self.events: dict[tuple[TaskId, str], list[api.LaneEvent]] = {}
        self.phases: dict[tuple[TaskId, str], list[api.LanePhase]] = {}

    def event(self, key: TaskId, lane: str, at: datetime, kind: str, detail: str):
        self.events.setdefault((key, lane), []).append(
            api.LaneEvent.model_validate(
                {"t": _iso(at), "kind": kind, "detail": detail}
            )
        )

    def phase(
        self,
        key: TaskId,
        lane: str,
        start: datetime,
        end: datetime | None,
        kind: str,
        detail: str,
        state: str = "",
    ):
        self.phases.setdefault((key, lane), []).append(
            api.LanePhase.model_validate(
                {
                    "start": _iso(start),
                    "end": _iso(end) if end else None,
                    "kind": kind,
                    "detail": detail,
                    "state": state,
                }
            )
        )


def build_lanes(
    store: StackStore,
    now: datetime,
    repo_slug: Callable[[int], str | None] = lambda repo_id: None,
) -> dict[TaskId, list[api.TimelineLane]]:
    """Every task's (tracked, and archived: the aggregate view reads them
    too) lanes that have something to draw, in the catalog's lane order, by
    task id (a branch name is reused, a task id is not). ``repo_slug`` is a
    repo's ``owner/name`` (what Slack posts are keyed by). A span still open
    when its task was archived ends at the archiving."""
    found = _Lanes()
    tasks: list[TrackedBranchFacts | RemovedBranch] = [
        *store.tracked_branch_facts(),
        *store.removed_branches(),
    ]
    archived_at = {
        facts.task_id: facts.removed_at
        for facts in tasks
        if isinstance(facts, RemovedBranch)
    }

    def end_of(task: TaskId, end: datetime | None) -> datetime | None:
        return end if end is not None else archived_at.get(task)

    # Observed changes: each value lasts until the next one is recorded.
    seen_lanes = {
        CONFLICT: ("git", _kinds("git", "MergeValue")),
        CI: ("ci", _kinds("ci", "CiState")),
        COMMENTS: ("review", _kinds("review", "CommentsValue")),
    }
    latest: dict[tuple[TaskId, str], tuple[datetime, str]] = {}
    for change in store.status_transitions():
        if change.task_id is None:
            continue
        key = change.task_id
        lane, kinds = seen_lanes[change.dimension]
        if (key, lane) in latest:
            start, value = latest[(key, lane)]
            for kind in kinds[value]:
                found.phase(key, lane, start, change.at, kind, "")
        latest[(key, lane)] = (change.at, change.value)
    kinds_of = dict(seen_lanes.values())
    for (key, lane), (start, value) in latest.items():
        for kind in kinds_of[lane][value]:
            found.phase(key, lane, start, end_of(key, None), kind, "")

    run_kinds = _kinds("agent", "AgentRunState")
    for run in store.agent_runs():
        detail = run.trigger.replace("_", " ")
        if run.result:
            detail += f" ({run.result})"
        if run.error:
            detail += f": {run.error}"
        elif run.summary:
            detail += f": {run.summary}"
        for kind in run_kinds[run.state]:
            found.phase(
                run.task_id, "agent", run.started_at, run.ended_at, kind, detail
            )

    holder_kinds = _kinds("claim", "HolderKind")
    for span in store.claim_spans():
        if span.task_id is None:
            continue
        kind = f"claim_{span.holder_kind}_{span.claim_kind}"
        assert kind in holder_kinds[span.holder_kind], kind
        live = span.last_seen >= now - SPAN_MAX_GAP
        found.phase(
            span.task_id,
            "claim",
            span.first_seen,
            end_of(span.task_id, None) if live else span.last_seen,
            kind,
            f"{span.holder}: {span.target}",
            span.state or "working",
        )

    posts: dict[tuple[str, int], list[SlackPostRow]] = {}
    for post in store.slack_posts():
        posts.setdefault((post.repo, post.pr_number), []).append(post)
    post_kinds = _kinds("slack", "SlackPostKind")
    for facts in tasks:
        slug = repo_slug(facts.repo_id)
        if facts.pr is None or slug is None:
            continue
        for post in posts.get((slug, facts.pr.number), []):
            for kind in post_kinds[post.kind]:
                found.event(
                    facts.task_id,
                    "slack",
                    post.posted_at,
                    kind,
                    post.assignee_name or "",
                )

    for row in store.merge_queue():
        if row.task_id is None:
            continue
        found.phase(
            row.task_id,
            "merge_queue",
            row.queued_at,
            end_of(row.task_id, None),
            "merge_queued",
            f"PR #{row.pr_number}",
        )

    decision_kinds = _kinds("decision", "DecisionStatus")
    for decision in store.decisions():
        start = datetime.fromisoformat(decision.created_at)
        answered = (
            datetime.fromisoformat(decision.answered_at)
            if decision.answered_at
            else None
        )
        for kind in decision_kinds[decision.status]:
            if kind == "decision_pending":
                found.phase(
                    decision.task_id,
                    "decision",
                    start,
                    end_of(decision.task_id, answered),
                    kind,
                    decision.title,
                )
            elif answered is not None:
                found.event(
                    decision.task_id, "decision", answered, kind, decision.title
                )

    decision_kinds_of_file = _kinds("personal_review", "FileApprovalState")
    for approval in store.file_approvals():
        for kind in decision_kinds_of_file[approval.state]:
            found.event(
                approval.task_id,
                "personal_review",
                approval.decided_at,
                kind,
                f"{approval.path}: {approval.note}" if approval.note else approval.path,
            )
    validate_runs: dict[TaskId, list[AgentRun]] = {}
    for run in store.agent_runs():
        if run.trigger == VALIDATE:
            validate_runs.setdefault(run.task_id, []).append(run)
    for reviewed, task_runs in validate_runs.items():
        since = validated_since(task_runs)
        if since is not None:
            found.phase(
                reviewed,
                "personal_review",
                since,
                end_of(reviewed, None),
                "personal_review_waiting",
                "",
            )

    stage_kinds = _kinds("task", "TaskStage")
    for task_id, stage, moved_at in store.task_status_moves():
        if task_id is None:
            continue
        for kind in stage_kinds[stage]:
            found.event(task_id, "task", moved_at, kind, "")

    lanes: dict[TaskId, list[api.TimelineLane]] = {}
    for facts in tasks:
        key = facts.task_id
        task_lanes = []
        for source in LANE_SOURCES:
            events = sorted(found.events.get((key, source.id), []), key=lambda e: e.t)
            phases = sorted(
                found.phases.get((key, source.id), []), key=lambda p: p.start
            )
            if events or phases:
                task_lanes.append(
                    api.TimelineLane(lane=source.id, events=events, phases=phases)  # type: ignore[arg-type]
                )
        lanes[key] = task_lanes
    return lanes
