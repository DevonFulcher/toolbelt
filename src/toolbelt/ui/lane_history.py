"""Reads each tracked branch's history for the lanes of its card's timeline
(``timeline_lanes.py``) from the stack database: observed conflict and CI
changes, agent runs, claim spans, Slack posts, the merge queue, decisions and
the task tracker moves.

Every kind a lane item gets comes from its source's ``Coverage`` (or the
source's own vocabulary), so a status value without a timeline kind is a
``KeyError`` here, not a missing item.
"""

from collections.abc import Callable
from datetime import datetime

from toolbelt.git.stack.store import (
    RemovedBranch,
    SlackPostRow,
    StackStore,
    TrackedBranchFacts,
)
from toolbelt.ui import api_models as api
from toolbelt.ui.claims_feed import SPAN_MAX_GAP
from toolbelt.ui.status_transitions import CI, CONFLICT
from toolbelt.ui.timeline_lanes import LANE_SOURCES, LANES

type BranchKey = tuple[int, str]


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _kinds(lane: str, status: str) -> dict[str, tuple[str, ...]]:
    (coverage,) = (c for c in LANES[lane].covers if c.status == status)
    return dict(coverage.kinds)


class _Lanes:
    """The items found so far, per branch and lane."""

    def __init__(self) -> None:
        self.events: dict[tuple[BranchKey, str], list[api.LaneEvent]] = {}
        self.phases: dict[tuple[BranchKey, str], list[api.LanePhase]] = {}

    def event(self, key: BranchKey, lane: str, at: datetime, kind: str, detail: str):
        self.events.setdefault((key, lane), []).append(
            api.LaneEvent.model_validate(
                {"t": _iso(at), "kind": kind, "detail": detail}
            )
        )

    def phase(
        self,
        key: BranchKey,
        lane: str,
        start: datetime,
        end: datetime | None,
        kind: str,
        detail: str,
    ):
        self.phases.setdefault((key, lane), []).append(
            api.LanePhase.model_validate(
                {
                    "start": _iso(start),
                    "end": _iso(end) if end else None,
                    "kind": kind,
                    "detail": detail,
                }
            )
        )


def build_lanes(
    store: StackStore,
    now: datetime,
    repo_slug: Callable[[int], str | None] = lambda repo_id: None,
) -> dict[BranchKey, list[api.TimelineLane]]:
    """Every branch's (tracked, and archived: the aggregate view reads them
    too) lanes that have something to draw, in the catalog's lane order. ``repo_slug`` is a repo's ``owner/name`` (what Slack
    posts are keyed by)."""
    found = _Lanes()
    tracked: list[TrackedBranchFacts | RemovedBranch] = [
        *store.tracked_branch_facts(),
        *store.removed_branches(),
    ]
    by_task = {facts.task_id: (facts.repo_id, facts.name) for facts in tracked}

    # Observed changes: each value lasts until the next one is recorded.
    git_kinds = _kinds("git", "MergeValue")
    ci_kinds = _kinds("ci", "CiState")
    latest: dict[tuple[BranchKey, str], tuple[datetime, str]] = {}
    for change in store.status_transitions():
        key = (change.repo_id, change.branch)
        lane, kinds = {CONFLICT: ("git", git_kinds), CI: ("ci", ci_kinds)}[
            change.dimension
        ]
        if (key, lane) in latest:
            start, value = latest[(key, lane)]
            for kind in kinds[value]:
                found.phase(key, lane, start, change.at, kind, "")
        latest[(key, lane)] = (change.at, change.value)
    for (key, lane), (start, value) in latest.items():
        for kind in (git_kinds if lane == "git" else ci_kinds)[value]:
            found.phase(key, lane, start, None, kind, "")

    run_kinds = _kinds("agent", "AgentRunState")
    for run in store.agent_runs():
        key = (run.repo_id, run.branch)
        detail = run.trigger.replace("_", " ")
        if run.error:
            detail += f": {run.error}"
        elif run.summary:
            detail += f": {run.summary}"
        for kind in run_kinds[run.state]:
            found.phase(key, "agent", run.started_at, run.ended_at, kind, detail)

    holder_kinds = _kinds("claim", "HolderKind")
    for span in store.claim_spans():
        claimed = by_task.get(span.task_id or "")
        if claimed is None:
            continue
        key = claimed
        kind = f"claim_{span.holder_kind}_{span.claim_kind}"
        assert kind in holder_kinds[span.holder_kind], kind
        live = span.last_seen >= now - SPAN_MAX_GAP
        found.phase(
            key,
            "claim",
            span.first_seen,
            None if live else span.last_seen,
            kind,
            f"{span.holder}: {span.target}",
        )

    posts: dict[tuple[str, int], list[SlackPostRow]] = {}
    for post in store.slack_posts():
        posts.setdefault((post.repo, post.pr_number), []).append(post)
    post_kinds = _kinds("slack", "SlackPostKind")
    for facts in tracked:
        slug = repo_slug(facts.repo_id)
        if facts.pr is None or slug is None:
            continue
        for post in posts.get((slug, facts.pr.number), []):
            for kind in post_kinds[post.kind]:
                found.event(
                    (facts.repo_id, facts.name),
                    "slack",
                    post.posted_at,
                    kind,
                    post.assignee_name or "",
                )

    for row in store.merge_queue():
        found.phase(
            (row.repo_id, row.branch),
            "merge_queue",
            row.queued_at,
            None,
            "merge_queued",
            f"PR #{row.pr_number}",
        )

    decision_kinds = _kinds("decision", "DecisionStatus")
    for decision in store.decisions():
        key = (decision.repo_id, decision.branch)
        start = datetime.fromisoformat(decision.created_at)
        answered = (
            datetime.fromisoformat(decision.answered_at)
            if decision.answered_at
            else None
        )
        for kind in decision_kinds[decision.status]:
            if kind == "decision_pending":
                found.phase(key, "decision", start, answered, kind, decision.title)
            elif answered is not None:
                found.event(key, "decision", answered, kind, decision.title)

    stage_kinds = _kinds("task", "TaskStage")
    for repo_id, branch, stage, moved_at in store.task_status_moves():
        for kind in stage_kinds[stage]:
            found.event((repo_id, branch), "task", moved_at, kind, "")

    lanes: dict[BranchKey, list[api.TimelineLane]] = {}
    for facts in tracked:
        key = (facts.repo_id, facts.name)
        branch_lanes = []
        for source in LANE_SOURCES:
            events = sorted(found.events.get((key, source.id), []), key=lambda e: e.t)
            phases = sorted(
                found.phases.get((key, source.id), []), key=lambda p: p.start
            )
            if events or phases:
                branch_lanes.append(
                    api.TimelineLane(lane=source.id, events=events, phases=phases)  # type: ignore[arg-type]
                )
        lanes[key] = branch_lanes
    return lanes
