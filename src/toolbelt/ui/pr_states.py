"""The one catalog of every state a `tt ui` card can show, and how the card's
timeline represents it.

A card's slots (PR, CI, Review, Comments, Merge, Next, ...) each show a value
of some *dimension*. The timeline (``web/src/components/Timeline.tsx``) must
show every one of them. That is enforced structurally: this module declares, for each
value of each dimension, how it appears on the track:

* ``event``: a marker at the moment it happened (kind from ``EVENTS``);
* ``phase``: a shaded span of the PR's life (kind from ``PHASES``);
* ``now_state``: a line ("Group: value") in the tooltip of the track's end
  cap (the "now" line, or the end of a merged/closed PR), for what has no
  event timestamp GitHub gives us (unresolved threads, conflicts) or is only
  meaningful now (current CI, the next action). The card's labelled slots
  show the same values; the timeline does not repeat them as a row of its
  own.

A value may have several representations (a failed CI is a ``commit_failed``
event, a ``ci_failing`` phase and a line in the end cap's tooltip). ``absent=True`` marks a value that
is shown by *not* drawing a phase at the track's end ("not a draft").

The catalog is served to the page inside the snapshot (``api_models.Catalog``,
which also generates the client's TypeScript types, so every dimension's
values are literal unions there): the renderer draws glyphs, colours and
labels from it, and the legend is generated from it, so neither can drift
from the cards. ``current_states``
turns a branch's current values into catalog references, looking each one up
(a missing entry is a ``KeyError``, not a silent blank). Adding a state
without a timeline representation fails ``tests/test_pr_states.py``.

What is deliberately *not* on the timeline is listed in ``NOT_REPRESENTED``
and shown in the legend.
"""

from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from typing import Literal, get_args

from toolbelt.git.stack.status import CiState, PrState, ReviewState
from toolbelt.ui import next_action as na

EVENT = "event"
PHASE = "phase"
NOW_STATE = "now_state"
HOWS = (EVENT, PHASE, NOW_STATE)

# Colour tokens; the page's stylesheet maps each to a colour variable.
type Tone = Literal["green", "red", "amber", "gray", "purple", "accent"]
GREEN: Tone = "green"
RED: Tone = "red"
AMBER: Tone = "amber"
GRAY: Tone = "gray"
PURPLE: Tone = "purple"
ACCENT: Tone = "accent"

# The keys of ``EVENTS`` / ``PHASES`` / ``CHIPS`` as types, so the wire models
# (and the client's TypeScript) can say which kinds exist. ``_index`` checks
# they are exactly those keys.
type EventKind = Literal[
    "started",
    "opened",
    "commit",
    "commits",
    "commit_failed",
    "force_push",
    "review_requested",
    "review_request_removed",
    "ready_for_review",
    "converted_to_draft",
    "review_approved",
    "review_changes",
    "review_commented",
    "merged",
    "closed",
    "reopened",
]
type PhaseKind = Literal[
    "working", "draft", "waiting", "ci_running", "ci_failing", "approved"
]
type ChipDimension = Literal["pr", "ci", "review", "comments", "merge", "next"]


@dataclass(frozen=True)
class Marker:
    label: str
    glyph: str
    tone: Tone
    legend: str


@dataclass(frozen=True)
class Span:
    label: str
    tone: Tone
    legend: str


@dataclass(frozen=True)
class Rep:
    """One way a value appears on the timeline. ``kind`` names an entry of
    ``EVENTS`` / ``PHASES``, or (for ``now_state``) a ``CHIPS`` dimension."""

    how: str
    kind: str
    absent: bool = False


@dataclass(frozen=True)
class StateValue:
    dimension: str
    value: str
    #: What the end cap's tooltip says for it; ``{n}`` is filled from the
    #: current state's args.
    label: str
    tone: Tone
    legend: str
    reps: tuple[Rep, ...]


#: Timeline markers, keyed by the marker kind the renderer derives from a
#: server event (``commit`` + a failed CI is ``commit_failed``, a review is
#: ``review_approved`` / ``review_changes`` / ``review_commented``).
EVENTS: dict[str, Marker] = {
    "started": Marker(
        "Task started",
        "◇",
        ACCENT,
        "The task started: its branch was created.",
    ),
    "opened": Marker("opened", "◆", GREEN, "The PR was opened."),
    "commit": Marker("commit", "•", GRAY, "A commit was pushed (faint dot)."),
    "commits": Marker(
        "commits",
        "",
        GRAY,
        "Several commits close together, grouped (the number is the count).",
    ),
    "commit_failed": Marker(
        "commit (CI failed)", "●", RED, "A commit whose CI ended failing."
    ),
    "force_push": Marker("force push", "↯", ACCENT, "The branch was force-pushed."),
    "review_requested": Marker(
        "review requested", "▸", AMBER, "A review was requested from someone."
    ),
    "review_request_removed": Marker(
        "request removed", "⊘", AMBER, "A review request was withdrawn."
    ),
    "ready_for_review": Marker(
        "ready for review", "▶", GREEN, "The draft was marked ready for review."
    ),
    "converted_to_draft": Marker(
        "back to draft", "◐", GRAY, "The PR was converted back to a draft."
    ),
    "review_approved": Marker("approved", "✓", GREEN, "A reviewer approved."),
    "review_changes": Marker(
        "changes requested", "✗", RED, "A reviewer requested changes."
    ),
    "review_commented": Marker(
        "commented", "✎", AMBER, "A reviewer left a review comment."
    ),
    "merged": Marker("merged", "⬤", PURPLE, "The PR was merged."),
    "closed": Marker("closed", "✕", RED, "The PR was closed without merging."),
    "reopened": Marker("reopened", "↺", GREEN, "The PR was reopened."),
}

#: Shaded spans of the PR's life.
PHASES: dict[str, Span] = {
    "working": Span(
        "Working (no PR yet)",
        ACCENT,
        "From the task's start until the PR was opened (or until now).",
    ),
    "draft": Span("In draft", GRAY, "Not yet ready for review."),
    "waiting": Span(
        "Waiting for review", AMBER, "Ready, with a review request outstanding."
    ),
    "ci_running": Span(
        "Waiting on CI",
        AMBER,
        "From a push until that commit's checks completed, whatever their "
        "outcome (CI takes time to run even when it fails).",
    ),
    "ci_failing": Span(
        "CI failing",
        RED,
        "From the checks completing with a failure until the next push. "
        "(Without a known completion time, from the push.)",
    ),
    "approved": Span(
        "Approved",
        GREEN,
        "From an approving review until merge or now. Ends early at the first "
        "push after it when the PR is no longer approved (a stale approval), "
        "or when that reviewer then requests changes.",
    ),
}

#: The groups of the "now" state (one line each in the end cap's tooltip, in
#: this order) and their labels.
CHIPS: dict[str, str] = {
    "pr": "PR",
    "ci": "CI",
    "review": "Review",
    "comments": "Comments",
    "merge": "Merge",
    "next": "Next",
}


def _v(dimension, value, label, tone, legend, *reps):
    return StateValue(dimension, value, label, tone, legend, tuple(reps))


def _ev(kind):
    return Rep(EVENT, kind)


def _ph(kind, absent=False):
    return Rep(PHASE, kind, absent)


def _now(dimension):
    return Rep(NOW_STATE, dimension)


_NEXT_TONES = {
    na.OPEN_PR: (AMBER, "No PR yet: open one."),
    na.DONE: (GRAY, "Merged or closed."),
    na.RESOLVE_CONFLICTS: (RED, "The PR has merge conflicts."),
    na.ADDRESS_REVIEW: (RED, "A reviewer requested changes."),
    na.RESOLVE_COMMENTS: (AMBER, "Unresolved review threads remain."),
    na.FIX_CI: (RED, "CI failed."),
    na.WAIT_CI: (AMBER, "CI is running."),
    na.PUBLISH: (ACCENT, "A draft with green CI: mark it ready."),
    na.BLOCKED: (GRAY, "Approved, but its parent PR is not merged yet."),
    na.MERGE: (GREEN, "Approved and ready to merge."),
    na.WAIT_REVIEW: (GRAY, "Waiting for a reviewer."),
    na.REQUEST_REVIEW: (ACCENT, "Ready, but nobody was asked to review."),
    na.POST_SLACK: (
        ACCENT,
        "Ready for review: post it in the review channel in Slack.",
    ),
    na.BUMP_SLACK: (
        AMBER,
        "Posted in Slack over a day ago and nobody has reviewed or commented: bump it.",
    ),
    na.CLAIMED: (
        PURPLE,
        (
            "An agent session has claimed this task and is working on it (a "
            "claim file; the action it replaced is in the tooltip)."
        ),
    ),
    na.AUTONOMOUS: (
        PURPLE,
        (
            "One of toolbelt's autonomous agents is working on this task (a "
            "claim file; the action it replaced is in the tooltip)."
        ),
    ),
}

_NEXT_LABELS = {
    na.OPEN_PR: "Open PR",
    na.DONE: "Done",
    na.RESOLVE_CONFLICTS: "Resolve conflicts",
    na.ADDRESS_REVIEW: "Address review",
    na.RESOLVE_COMMENTS: "Resolve comments",
    na.FIX_CI: "Fix / re-run CI",
    na.WAIT_CI: "Waiting on CI",
    na.PUBLISH: "Publish PR in GitHub",
    na.BLOCKED: "Blocked on parent",
    na.MERGE: "Merge",
    na.WAIT_REVIEW: "Waiting for review",
    na.REQUEST_REVIEW: "Request review",
    na.POST_SLACK: "Post in Slack",
    na.BUMP_SLACK: "Bump in Slack",
    na.CLAIMED: "With an agent",
    na.AUTONOMOUS: "Autonomous agent",
}

STATES: tuple[StateValue, ...] = (
    # PR slot (and the title slot: the opened event carries the title).
    _v(
        "pr",
        PrState.OPEN.value,
        "open",
        GREEN,
        "An open PR.",
        _ev("opened"),
        _now("pr"),
    ),
    _v(
        "pr",
        PrState.DRAFT.value,
        "draft",
        GRAY,
        "A draft PR.",
        _ph("draft"),
        _now("pr"),
    ),
    _v(
        "pr",
        PrState.MERGED.value,
        "merged",
        PURPLE,
        "Merged.",
        _ev("merged"),
        _now("pr"),
    ),
    _v(
        "pr",
        PrState.CLOSED.value,
        "closed",
        RED,
        "Closed without merging.",
        _ev("closed"),
        _now("pr"),
    ),
    _v(
        "pr",
        PrState.NONE.value,
        "no PR",
        GRAY,
        "The branch has no PR yet (its working span is drawn from the task's "
        "start, when that is known).",
        _now("pr"),
    ),
    # CI slot and the CI failures slot (one commit_failed marker per failure).
    _v(
        "ci",
        CiState.SUCCESS.value,
        "passing",
        GREEN,
        "The head commit's checks passed.",
        _now("ci"),
    ),
    _v(
        "ci",
        CiState.FAILED.value,
        "failed",
        RED,
        "The head commit's checks failed.",
        _ev("commit_failed"),
        _ph("ci_failing"),
        _now("ci"),
    ),
    _v(
        "ci",
        CiState.RUNNING.value,
        "running",
        AMBER,
        "Checks are still running.",
        _ph("ci_running"),
        _now("ci"),
    ),
    _v(
        "ci", CiState.NONE.value, "no CI", GRAY, "No checks are configured.", _now("ci")
    ),
    # Review slot.
    _v(
        "review",
        ReviewState.APPROVED.value,
        "approved",
        GREEN,
        "Approved by a reviewer.",
        _ev("review_approved"),
        _ph("approved"),
        _now("review"),
    ),
    _v(
        "review",
        ReviewState.REQUESTED.value,
        "requested",
        AMBER,
        "A review is requested and pending.",
        _ev("review_requested"),
        _ph("waiting"),
        _now("review"),
    ),
    _v(
        "review",
        ReviewState.REVIEWED.value,
        "changes requested",
        RED,
        "A reviewer requested changes.",
        _ev("review_changes"),
        _now("review"),
    ),
    _v(
        "review",
        ReviewState.NONE.value,
        "none requested",
        GRAY,
        "Nobody was asked to review.",
        _now("review"),
    ),
    # Comments slot: GitHub gives no timestamp for a thread opening or
    # resolving, so the count is a now-state only.
    _v(
        "comments",
        "none",
        "0 unresolved",
        GRAY,
        "No unresolved review threads.",
        _now("comments"),
    ),
    _v(
        "comments",
        "open",
        "{n} unresolved",
        AMBER,
        "Unresolved review threads (current count, no history).",
        _now("comments"),
    ),
    # Merge slot: likewise, "became conflicting" is not an event.
    _v("merge", "clean", "clean", GREEN, "No merge conflicts.", _now("merge")),
    _v(
        "merge",
        "conflicts",
        "conflicts",
        RED,
        "The PR conflicts with its base (current state, no history).",
        _now("merge"),
    ),
    _v("merge", "none", "n/a", GRAY, "Not applicable: no open PR.", _now("merge")),
    # Task age slot: when the task started (when the branch was registered).
    _v(
        "task",
        "started",
        "task started",
        ACCENT,
        "The task's start is known: the track begins there, with a working "
        "span until the PR opened.",
        _ev("started"),
        _ph("working"),
    ),
    _v(
        "task",
        "unknown",
        "no start recorded",
        GRAY,
        "No start was recorded: the track begins at the PR's earliest activity.",
        Rep(EVENT, "started", absent=True),
        _ph("working", absent=True),
    ),
    # In draft slot.
    _v(
        "draft",
        "draft",
        "in draft",
        GRAY,
        "Currently a draft.",
        _ph("draft"),
        _ev("converted_to_draft"),
    ),
    _v(
        "draft",
        "ready",
        "not a draft",
        GRAY,
        "Not a draft: no draft span at the track's end.",
        _ph("draft", absent=True),
        _ev("ready_for_review"),
    ),
    # Review requested / Reviewers slots.
    _v(
        "reviewers",
        "pending",
        "reviewers pending",
        AMBER,
        "At least one review request is outstanding.",
        _ph("waiting"),
        _ev("review_requested"),
    ),
    _v(
        "reviewers",
        "none",
        "none pending",
        GRAY,
        "No review request outstanding: no waiting span at the end.",
        _ph("waiting", absent=True),
        _ev("review_request_removed"),
    ),
    # Blocked-on-parent is a Next action kind; this says so explicitly.
    _v(
        "blocked",
        "yes",
        "blocked",
        GRAY,
        "Approved but its parent is unmerged: the Next line says so.",
        _now("next"),
    ),
    _v(
        "blocked",
        "no",
        "not blocked",
        GRAY,
        "Not waiting on a parent PR.",
        Rep(NOW_STATE, "next", absent=True),
    ),
    # Next slot: one value per rule.
    *(
        _v("next", kind, _NEXT_LABELS[kind], tone, legend, _now("next"))
        for kind, (tone, legend) in _NEXT_TONES.items()
    ),
)

#: Which dimension each card slot shows (``None``: not a PR state; the
#: reason says why it needs no timeline representation). Served in the
#: catalog, so the client's card cannot render a slot that is not listed here
#: (a compile error there).
SLOT_STATES: dict[str, str | None] = {
    "next": "next",
    "pr": "pr",
    "ci": "ci",
    "review": "review",
    "comments": "comments",
    "bot_comments": "comments",
    "merge": "merge",
    "conflict_commit": "merge",
    "in_draft": "draft",
    "review_requested": "reviewers",
    "reviewers": "reviewers",
    "ci_failures": "ci",
    "title": "pr",
    "task_age": "task",
    "pr_age": "pr",
    "timeline": "pr",
    # Not PR states: where the branch is, what it is linked to, and how big
    # the PR is.
    "lines": None,
    "agent_status": None,
    "agent_run": None,
    "agent_run_summary": None,
    "cost": None,
    "agent_cost": None,
    "slack_status": None,
    "slack_age": None,
    "repo": None,
    "branch": None,
    "task": None,
    "task_title": None,
    "task_status": None,
    "session": None,
    "ancestors": None,
    "descendants": None,
}

NOT_REPRESENTED: tuple[tuple[str, str], ...] = (
    (
        "Comment threads opening or resolving",
        "GitHub has no timeline event for it; only the current unresolved count is shown (Comments, in the Now tooltip).",
    ),
    (
        "A PR becoming conflicting",
        "Not an event; only whether it conflicts now is shown (Merge, in the Now tooltip).",
    ),
    (
        "CI progress between commits",
        "Only each commit's final result is known; a re-run of the same commit is not an event.",
    ),
    (
        "When an approval went stale or was dismissed",
        "Inferred: it ends at the first push after it when the PR is not approved now.",
    ),
    (
        "History of being blocked on a parent",
        "Derived from the parent's state now (Next, in the Now tooltip), not recorded.",
    ),
    (
        "History of an agent claiming a task",
        (
            "Claims are files with no history: With an agent shows only now, and "
            "the aggregate view does not count it (the task is in the state its "
            "PR was in)."
        ),
    ),
    (
        "Conversation comments, edits, labels, assignees",
        "Not review activity; not fetched.",
    ),
)


def _index() -> dict[tuple[str, str], StateValue]:
    for literal, keys in (
        (EventKind, EVENTS),
        (PhaseKind, PHASES),
        (ChipDimension, CHIPS),
    ):
        assert set(get_args(literal.__value__)) == set(keys), f"{literal} out of step"
    index: dict[tuple[str, str], StateValue] = {}
    for state in STATES:
        key = (state.dimension, state.value)
        assert key not in index, f"duplicate state {key}"
        index[key] = state
        assert state.reps, f"{key} has no timeline representation"
        for rep in state.reps:
            assert rep.how in HOWS, f"{key}: unknown how {rep.how}"
            known: Mapping[str, Collection[str]] = {
                EVENT: EVENTS,
                PHASE: PHASES,
                NOW_STATE: CHIPS,
            }
            assert rep.kind in known[rep.how], (
                f"{key}: unknown {rep.how} kind {rep.kind}"
            )
    return index


_INDEX = _index()


def lookup(dimension: str, value: str) -> StateValue:
    return _INDEX[(dimension, value)]


def current_states(
    *,
    pr: str,
    ci: str,
    review: str,
    open_comments: int,
    merge: str,
    next_kind: str,
    next_who: str,
    next_label: str,
    next_would_be: str | None = None,
) -> list[dict]:
    """The card's current state (listed in the end cap's tooltip): references into the catalog (each
    looked up here, so an unknown value raises), with the actual label of the
    next action (it names the parent when blocked)."""
    has_pr = pr != PrState.NONE.value
    refs: list[tuple[str, str, dict]] = [("pr", pr, {})]
    if has_pr:
        refs += [
            ("ci", ci, {}),
            ("review", review, {}),
            ("comments", "open" if open_comments else "none", {"n": open_comments}),
            ("merge", merge, {}),
        ]
    refs.append(
        (
            "next",
            next_kind,
            {"text": next_label, "who": next_who, "would_be": next_would_be},
        )
    )
    states = []
    for dimension, value, args in refs:
        lookup(dimension, value)
        states.append({"dimension": dimension, "value": value, "args": args})
    return states


def catalog() -> dict:
    return {
        "events": {kind: asdict(marker) for kind, marker in EVENTS.items()},
        "phases": {kind: asdict(span) for kind, span in PHASES.items()},
        "chips": CHIPS,
        "slots": SLOT_STATES,
        "states": [asdict(state) for state in STATES],
        "not_represented": [
            {"label": label, "why": why} for label, why in NOT_REPRESENTED
        ],
    }
