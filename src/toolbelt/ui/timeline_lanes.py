"""The lanes of a card's timeline, and what each one can draw.

The top track of a card's timeline is the PR's own history (``pr_states``). Every
other source of status the card knows about (git conflicts, observed CI,
autonomous agent runs, claims, Slack posts, the merge queue, decisions and the
task tracker) is a *lane* drawn under it on the same time axis. A lane is a
``LaneSource``: its label, the markers (``events``) and shaded spans
(``phases``) it can draw, and ``covers``, the status types it draws.

Adding a status therefore has one place to go. A new value of a covered type
fails ``tests/test_timeline_lanes.py`` until it maps to a kind of the lane, and
a new status type in ``api_models`` fails it until a lane covers it (or
``NOT_TIMELINE_STATUS`` says why it has no history). The kinds below are the
wire's ``LaneEventKind`` / ``LanePhaseKind`` and the client's
``catalog.lane_events`` / ``catalog.lane_phases``, so the renderer and the
legend draw them without per-kind code. The history itself is read in
``lane_history.py``.

Status types are named (they are ``type`` aliases in ``api_models``, which
builds its wire types from this module).
"""

from collections.abc import Mapping
from dataclasses import dataclass

from toolbelt.ui.pr_states import ACCENT, AMBER, GRAY, GREEN, PURPLE, RED, Marker, Span


@dataclass(frozen=True)
class Coverage:
    """How a lane draws the values of the status type named ``status``:
    ``kinds`` maps each value to the kinds (events or phases of the lane) that
    show it, an empty tuple meaning the value is shown by *not* drawing (no
    conflict: nothing under the line). ``detail``: the type has no kinds of
    its own; its value is the text in the tooltip of the items that carry it."""

    status: str
    kinds: Mapping[str, tuple[str, ...]]
    detail: bool = False


@dataclass(frozen=True)
class LaneSource:
    id: str
    label: str
    events: Mapping[str, Marker]
    phases: Mapping[str, Span]
    covers: tuple[Coverage, ...]
    #: Why the aggregate view does not draw a layer for this lane; ``None``:
    #: it does (see ``aggregate.py``).
    aggregate_skip: str | None = None


def _run_phase(state: str) -> tuple[str, ...]:
    return (f"agent_{state}",)


def _claim_phases(holder: str) -> tuple[str, ...]:
    return (f"claim_{holder}_worktree", f"claim_{holder}_ref")


LANE_SOURCES: tuple[LaneSource, ...] = (
    LaneSource(
        id="git",
        label="Git",
        events={},
        phases={
            "git_conflicting": Span(
                "Conflicting",
                RED,
                "The PR conflicted with its base, as `tt ui` saw it (the start is "
                "when it was first seen).",
            ),
        },
        covers=(
            Coverage(
                "MergeValue",
                {"conflicts": ("git_conflicting",), "clean": (), "none": ()},
            ),
        ),
    ),
    LaneSource(
        id="ci",
        label="CI (seen)",
        aggregate_skip="The status mix replays CI from GitHub's own history of each commit, which is complete; this lane only holds what `tt ui` saw.",
        events={},
        phases={
            "ci_seen_running": Span(
                "CI running", AMBER, "The PR's checks were running, as `tt ui` saw."
            ),
            "ci_seen_failed": Span(
                "CI failed", RED, "The PR's checks were failing, as `tt ui` saw."
            ),
            "ci_seen_success": Span(
                "CI passed", GREEN, "The PR's checks had passed, as `tt ui` saw."
            ),
        },
        covers=(
            Coverage(
                "CiState",
                {
                    "none": (),
                    "running": ("ci_seen_running",),
                    "failed": ("ci_seen_failed",),
                    "success": ("ci_seen_success",),
                },
            ),
        ),
    ),
    LaneSource(
        id="agent",
        label="Agent runs",
        events={},
        phases={
            "agent_running": Span(
                "Agent working", ACCENT, "An autonomous agent run was working on it."
            ),
            "agent_deciding": Span(
                "Agent asked you",
                AMBER,
                "A run asked a question and waited for the answer.",
            ),
            "agent_succeeded": Span(
                "Agent run done", GREEN, "An autonomous agent run that finished."
            ),
            "agent_failed": Span(
                "Agent run failed", RED, "An autonomous agent run that failed."
            ),
            "agent_timed_out": Span(
                "Agent run timed out", RED, "A run that hit its time limit."
            ),
            "agent_cap_reached": Span(
                "Agent run capped", RED, "A run that hit its turn or cost cap."
            ),
            "agent_stopped": Span("Agent run stopped", GRAY, "A run you stopped."),
            "agent_abandoned": Span(
                "Agent run abandoned",
                GRAY,
                "A run `tt ui` lost track of (it restarted while the run was working).",
            ),
        },
        covers=(
            Coverage(
                "AgentRunState",
                {
                    state: _run_phase(state)
                    for state in (
                        "running",
                        "deciding",
                        "succeeded",
                        "failed",
                        "timed_out",
                        "cap_reached",
                        "stopped",
                        "abandoned",
                    )
                },
            ),
            Coverage("AgentTrigger", {}, detail=True),
        ),
    ),
    LaneSource(
        id="claim",
        label="Claims",
        events={},
        phases={
            "claim_agent_worktree": Span(
                "Agent session (worktree)",
                PURPLE,
                "An interactive agent session held a claim on the task's worktree.",
            ),
            "claim_agent_ref": Span(
                "Agent session (ref)",
                PURPLE,
                "An interactive agent session claimed the task by branch or ticket, "
                "without editing its worktree.",
            ),
            "claim_human_worktree": Span(
                "You (worktree)", ACCENT, "A human held a claim on the worktree."
            ),
            "claim_human_ref": Span(
                "You (ref)", ACCENT, "A human claimed the task by branch or ticket."
            ),
            "claim_autonomous_worktree": Span(
                "Autonomous (worktree)",
                AMBER,
                "An autonomous agent held a claim on the worktree.",
            ),
            "claim_autonomous_ref": Span(
                "Autonomous (ref)",
                AMBER,
                "An autonomous agent claimed the task by branch or ticket.",
            ),
        },
        covers=(
            Coverage(
                "HolderKind",
                {
                    holder: _claim_phases(holder)
                    for holder in ("agent", "human", "autonomous")
                },
            ),
            Coverage(
                "ClaimKind",
                {"worktree": (), "ref": ()},
                detail=True,
            ),
        ),
    ),
    LaneSource(
        id="slack",
        label="Slack",
        events={
            "slack_post": Marker(
                "posted in Slack", "✉", ACCENT, "The PR was posted in Slack."
            ),
            "slack_bump": Marker(
                "bumped in Slack", "↑", AMBER, "The PR's Slack thread was bumped."
            ),
            "slack_reassign": Marker(
                "reassigned in Slack",
                "⇄",
                AMBER,
                "The PR's Slack thread asked someone else to review.",
            ),
        },
        phases={},
        covers=(
            Coverage(
                "SlackPostKind",
                {
                    "post": ("slack_post",),
                    "bump": ("slack_bump",),
                    "reassign": ("slack_reassign",),
                },
            ),
        ),
    ),
    LaneSource(
        id="merge_queue",
        label="Merge queue",
        events={},
        phases={
            "merge_queued": Span(
                "Queued to merge",
                PURPLE,
                "Queued: it merges when its CI has passed.",
            ),
        },
        covers=(),
    ),
    LaneSource(
        id="decision",
        label="Decisions",
        events={
            "decision_answered": Marker(
                "decision answered", "?", GREEN, "You answered an agent's question."
            ),
        },
        phases={
            "decision_pending": Span(
                "Waiting for your decision",
                AMBER,
                "An agent asked you a question about this task and waited.",
            ),
        },
        covers=(
            Coverage(
                "DecisionStatus",
                {
                    "pending": ("decision_pending",),
                    "answered": ("decision_pending", "decision_answered"),
                },
            ),
        ),
    ),
    LaneSource(
        id="task",
        label="Task tracker",
        events={
            "task_in_review": Marker(
                "task moved to In Review",
                "◈",
                GREEN,
                "`tt ui` moved the task to In Review.",
            ),
        },
        phases={},
        covers=(
            Coverage(
                "TaskStage",
                {"in_progress": (), "in_review": ("task_in_review",), "done": ()},
            ),
        ),
    ),
)

LANES: dict[str, LaneSource] = {source.id: source for source in LANE_SOURCES}

#: Every lane event / phase kind, with its marker / span. Kinds are unique
#: across lanes (each is prefixed by what it belongs to).
LANE_EVENTS: dict[str, Marker] = {
    kind: marker for source in LANE_SOURCES for kind, marker in source.events.items()
}
LANE_PHASES: dict[str, Span] = {
    kind: span for source in LANE_SOURCES for kind, span in source.phases.items()
}

assert len(LANE_EVENTS) == sum(len(s.events) for s in LANE_SOURCES), (
    "duplicate event kind"
)
assert len(LANE_PHASES) == sum(len(s.phases) for s in LANE_SOURCES), (
    "duplicate phase kind"
)

#: Status types in ``api_models`` (or its imports) that no lane draws, and why.
#: The type's name, then the reason. A type not here and not covered by a lane
#: or by the PR track (``pr_states``) fails ``tests/test_timeline_lanes.py``.
NOT_TIMELINE_STATUS: dict[str, str] = {
    "HolderState": "Claim spans record that a claim was held, not whether its holder was working, idle or blocked; the live card shows that.",
    "AgentReasonCode": "Why no agent is on a task is a decision of the moment, not recorded.",
    "FilterKey": "A filter's name, not a status.",
    "MixState": "The aggregate view's bands, drawn from the PR catalog.",
    "MixWho": "Who the aggregate view's bands wait on.",
    "CiResult": "A commit's CI result: drawn by the PR track's commit markers.",
    "Tone": "A colour token, not a status.",
    "Who": "Who a next action waits on: shown as the card's Next chip.",
    "ReviewStatus": "How a file changed between two revisions in the diff review: git, not a task's history.",
    "ReviewSide": "Which side of a diff a position is on: not a status.",
    "DifftasticStatus": "How difftastic classed a file pair in the diff review: not a task's history.",
}
