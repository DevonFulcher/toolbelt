"""The wire types of `tt ui`'s JSON API: the one definition of what the server
sends and what the web client (``web/``) reads.

Two things are served, both made of the models below:

* ``GET /api/snapshot``: ``Snapshot``, every repo's branches (stack structure,
  links, live status) plus the state catalog;
* ``GET /api/events``: a Server-Sent Events stream of ``ServerEvent``s that
  keep a snapshot current (a branch's new status, a task's title, a structure
  change, ...);
* ``GET /api/aggregate``: ``Aggregate``, how work has flowed over a window
  (throughput, lead times, the status mix), computed on demand.

TypeScript types are generated from these models (``api_schema.py`` writes
their JSON Schema, ``web/scripts/gen-types.mjs`` turns it into
``web/src/api/generated.ts``), so enums and ``Literal``s become literal
unions on the client: a value added here is a compile error there until the
client handles it. Tests fail when the committed generated files are stale.

Derivations stay in Python (next action, ages, the catalog); the client does
presentation, sorting, filtering, grouping and layout.
"""

from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from toolbelt.git.stack.status import CiState, PrState, ReviewState
from toolbelt.task_claims.claim_file import HolderKind, HolderState
from toolbelt.tasks.tracker import Stage as TaskStage  # noqa: F401  (a status type the task lane covers by this name)
from toolbelt.ui import pr_states, timeline_lanes
from toolbelt.ui.next_action import NextKind, Who
from toolbelt.ui.pr_activity import ActivityKind
from toolbelt.ui.pr_states import ChipDimension, EventKind, PhaseKind, Tone


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# Named in the generated schema (as ``type`` aliases) so the client's types
# have these names too.
type CommentsValue = Literal["none", "open"]
type MergeValue = Literal["conflicts", "clean", "none"]
type CiResult = Literal["", "failed", "success", "pending"]
type FilterKey = Literal["next", "pr", "ci", "review", "comments", "merge"]
type ClaimKind = Literal["worktree", "ref"]
type SlackPostKind = Literal["post", "bump", "reassign"]
type DecisionStatus = Literal["pending", "answered"]
type FileApprovalState = Literal["approved", "changes_requested"]

# The lanes under the PR track (see timeline_lanes.py): the kinds of their
# markers and spans, built from the registry so a kind added there is a kind
# here and in the client's types.
type LaneId = Literal[*timeline_lanes.LANES]  # type: ignore[valid-type]
type LaneEventKind = Literal[*timeline_lanes.LANE_EVENTS]  # type: ignore[valid-type]
type LanePhaseKind = Literal[*timeline_lanes.LANE_PHASES]  # type: ignore[valid-type]


# ---- the state catalog (see pr_states.py) ------------------------------------


class Marker(Model):
    label: str
    glyph: str
    tone: Tone
    legend: str


class Span(Model):
    label: str
    tone: Tone
    legend: str


class EventRep(Model):
    how: Literal["event"]
    kind: EventKind
    absent: bool


class PhaseRep(Model):
    how: Literal["phase"]
    kind: PhaseKind
    absent: bool


class NowRep(Model):
    how: Literal["now_state"]
    kind: ChipDimension
    absent: bool


type Rep = Annotated[EventRep | PhaseRep | NowRep, Field(discriminator="how")]


class _CatalogState(Model):
    #: What a chip says; ``{n}`` is filled from the current state.
    label: str
    tone: Tone
    legend: str
    reps: list[Rep]


class PrCatalogState(_CatalogState):
    dimension: Literal["pr"]
    value: PrState


class CiCatalogState(_CatalogState):
    dimension: Literal["ci"]
    value: CiState


class ReviewCatalogState(_CatalogState):
    dimension: Literal["review"]
    value: ReviewState


class CommentsCatalogState(_CatalogState):
    dimension: Literal["comments"]
    value: CommentsValue


class MergeCatalogState(_CatalogState):
    dimension: Literal["merge"]
    value: MergeValue


class TaskCatalogState(_CatalogState):
    dimension: Literal["task"]
    value: Literal["started", "unknown"]


class DraftCatalogState(_CatalogState):
    dimension: Literal["draft"]
    value: Literal["draft", "ready"]


class ReviewersCatalogState(_CatalogState):
    dimension: Literal["reviewers"]
    value: Literal["pending", "none"]


class BlockedCatalogState(_CatalogState):
    dimension: Literal["blocked"]
    value: Literal["yes", "no"]


class NextCatalogState(_CatalogState):
    dimension: Literal["next"]
    value: NextKind


type CatalogState = Annotated[
    PrCatalogState
    | CiCatalogState
    | ReviewCatalogState
    | CommentsCatalogState
    | MergeCatalogState
    | TaskCatalogState
    | DraftCatalogState
    | ReviewersCatalogState
    | BlockedCatalogState
    | NextCatalogState,
    Field(discriminator="dimension"),
]

# One required field per kind, so the client's types say ``events.opened``
# exists and ``events[kind]`` (``kind: EventKind``) can never be undefined.
EventMarkers = create_model(
    "EventMarkers",
    __base__=Model,
    **{kind: (Marker, ...) for kind in pr_states.EVENTS},  # type: ignore[call-overload]
)
PhaseSpans = create_model(
    "PhaseSpans",
    __base__=Model,
    **{kind: (Span, ...) for kind in pr_states.PHASES},  # type: ignore[call-overload]
)


class ChipGroups(Model):
    """The label of each "now" chip group."""

    pr: str
    ci: str
    review: str
    comments: str
    merge: str
    next: str


type CatalogDimension = Literal[
    "pr",
    "ci",
    "review",
    "comments",
    "merge",
    "task",
    "draft",
    "reviewers",
    "blocked",
    "next",
]

# Which dimension each card slot shows (``None``: not a PR state). One
# required field per slot, so the client can only render slots listed here.
SlotDimensions = create_model(
    "SlotDimensions",
    __base__=Model,
    **{slot: (CatalogDimension | None, ...) for slot in pr_states.SLOT_STATES},  # type: ignore[call-overload]
)


class NotRepresented(Model):
    label: str
    why: str


LaneEventMarkers = create_model(
    "LaneEventMarkers",
    __base__=Model,
    **{kind: (Marker, ...) for kind in timeline_lanes.LANE_EVENTS},  # type: ignore[call-overload]
)
LanePhaseSpans = create_model(
    "LanePhaseSpans",
    __base__=Model,
    **{kind: (Span, ...) for kind in timeline_lanes.LANE_PHASES},  # type: ignore[call-overload]
)


class LaneSpec(Model):
    id: LaneId
    label: str
    #: The kinds of ``lane_events`` / ``lane_phases`` this lane draws.
    events: list[LaneEventKind]
    phases: list[LanePhaseKind]


class Catalog(Model):
    events: EventMarkers  # type: ignore[valid-type]
    phases: PhaseSpans  # type: ignore[valid-type]
    #: The lanes under the PR track, in drawing order, and the vocabulary of
    #: their markers and spans.
    lanes: list[LaneSpec]
    lane_events: LaneEventMarkers  # type: ignore[valid-type]
    lane_phases: LanePhaseSpans  # type: ignore[valid-type]
    chips: ChipGroups
    slots: SlotDimensions  # type: ignore[valid-type]
    states: list[CatalogState]
    not_represented: list[NotRepresented]


def build_catalog() -> Catalog:
    """The catalog of ``pr_states``, validated into the wire model (so a
    value the types do not know about is an error here, not a blank on the
    page)."""
    return Catalog.model_validate(
        {
            **pr_states.catalog(),
            "lanes": [
                {
                    "id": source.id,
                    "label": source.label,
                    "events": list(source.events),
                    "phases": list(source.phases),
                }
                for source in timeline_lanes.LANE_SOURCES
            ],
            "lane_events": {
                kind: asdict(marker)
                for kind, marker in timeline_lanes.LANE_EVENTS.items()
            },
            "lane_phases": {
                kind: asdict(span) for kind, span in timeline_lanes.LANE_PHASES.items()
            },
        }
    )


# ---- a branch's live status ---------------------------------------------------


class Age(Model):
    """A duration for display. ``since`` (ISO time) is set while it is still
    growing, so the page can keep the text current; ``None`` once it ended."""

    text: str
    since: str | None


class TimelineEvent(Model):
    """One event of a PR's history, in time order. ``ci`` is, for a commit
    (or the commit a force push replaced), its CI result (``""``: none)."""

    t: str
    kind: ActivityKind
    actor: str
    detail: str
    ci: CiResult
    #: The reviewer a review request concerns, or who submitted a review.
    subject: str
    #: For a commit: when its checks completed, if known. The CI phases
    #: (waiting on CI, then failing) are drawn from it.
    ci_done: str | None


class PrDetail(Model):
    title: str
    pr_age: Age
    draft_age: Age | None
    review_request_age: Age | None
    ci_failures: int
    #: Lines the PR adds and deletes against its base (its parent branch for a
    #: stacked PR).
    additions: int
    deletions: int
    #: ISO time the PR was created, when it ended (merged/closed; ``None``
    #: while open) and whether it is a draft: what the per-card timeline
    #: track is scaled and shaded from.
    created: str
    ended: str | None
    is_draft: bool
    events: list[TimelineEvent]


class WouldBeAction(Model):
    """What a claimed branch's next action would be without the claim."""

    label: str
    who: Who
    kind: NextKind


class NextActionInfo(Model):
    label: str
    #: Who is blocking progress.
    who: Who
    #: Which rule fired (see ``next_action.py``).
    kind: NextKind
    #: For "With an agent" (``kind == "claimed"``): the action it replaced.
    would_be: WouldBeAction | None = None


class PrChip(Model):
    dimension: Literal["pr"]
    value: PrState


class CiChip(Model):
    dimension: Literal["ci"]
    value: CiState


class ReviewChip(Model):
    dimension: Literal["review"]
    value: ReviewState


class CommentsChip(Model):
    dimension: Literal["comments"]
    value: CommentsValue
    #: Unresolved threads.
    n: int


class MergeChip(Model):
    dimension: Literal["merge"]
    value: MergeValue


class NextChip(Model):
    dimension: Literal["next"]
    value: NextKind
    #: The action's own label (it names the parent when blocked).
    text: str
    who: Who
    #: For "With an agent": the label of the action it replaced.
    would_be: str | None = None


type Chip = Annotated[
    PrChip | CiChip | ReviewChip | CommentsChip | MergeChip | NextChip,
    Field(discriminator="dimension"),
]


class LoadingStatus(Model):
    """No result in yet (or the repo's checkout is missing: nothing to ask)."""

    load: Literal["loading"]


class FailedStatus(Model):
    """The lookup was attempted and failed."""

    load: Literal["failed"]


class ConflictInfo(Model):
    """The commit a branch's conflict comes from."""

    sha: str
    subject: str
    #: Where to open the commit on GitHub (``None`` when the PR is unknown).
    url: str | None
    #: The files that conflict, at most ten.
    files: list[str]
    #: ``pr``: the first commit of the PR that conflicts with its base;
    #: ``rebase`` / ``merge``: what the branch's worktree is stopped on.
    source: Literal["pr", "rebase", "merge"]
    #: For a stopped rebase: which commit this is, of how many.
    step: int | None
    steps: int | None


class LoadedStatus(Model):
    load: Literal["loaded"]
    pr: PrState
    pr_number: int | None
    pr_url: str | None
    ci: CiState
    review: ReviewState
    #: Only meaningful for an open PR (``none`` otherwise).
    merge: MergeValue
    open_comments: int
    #: How many of those a bot (Copilot) started; the rest are a person's.
    open_comments_bot: int
    next: NextActionInfo
    #: Where the conflict comes from, when the branch has one.
    conflict: ConflictInfo | None
    #: The PR's history; ``None`` when there is no PR.
    detail: PrDetail | None
    #: The "now" chips of the card's timeline.
    now: list[Chip]


type LiveStatus = Annotated[
    LoadingStatus | FailedStatus | LoadedStatus, Field(discriminator="load")
]


# ---- a branch's task ----------------------------------------------------------


class TaskLoading(Model):
    state: Literal["loading"]


class TaskDetails(Model):
    state: Literal["info"]
    title: str
    status: str


class TaskUnavailable(Model):
    state: Literal["unavailable"]
    #: Why the tracker could not say (shown as a tooltip).
    reason: str


type TaskLive = Annotated[
    TaskLoading | TaskDetails | TaskUnavailable, Field(discriminator="state")
]


# ---- the snapshot ---------------------------------------------------------------


class Link(Model):
    """``href`` is ``None`` when the value cannot (or should not) be linked."""

    label: str
    href: str | None


class HolderActivity(Model):
    """What a claim's holder is running: the tool call in flight (``open``),
    else the last that ended."""

    tool: str
    #: The command or path, cut short.
    detail: str
    #: ISO time the call started.
    since: str
    open: bool


class ClaimHolder(Model):
    """A live holder of a claim on a branch (see ``task_claims``). Only live
    holders are sent: an expired one is gone."""

    holder: str
    kind: HolderKind
    #: ``working`` makes the branch "With an agent"; ``idle`` (waiting for
    #: you) and ``blocked`` (waiting on a prompt) only add a chip.
    state: HolderState
    #: A deeplink to the holder's session, when it has one.
    link: Link | None
    #: How long it has held the task, counting from ``since``.
    age: Age
    #: Working, but not heard from for a while: probably hung.
    stale: bool
    #: What it is running, when the plugin says.
    activity: HolderActivity | None = None


class BranchClaim(Model):
    holders: list[ClaimHolder]


class Repo(Model):
    id: int
    name: str
    path: str
    path_missing: bool


class ChildRef(Model):
    id: str
    name: str


class Branch(Model):
    #: Unique across repos (``main`` can be in several): ``"<repo id>:<name>"``.
    id: str
    repo_id: int
    name: str
    #: Stack depth: 0 for a branch directly on the trunk (which is not a
    #: tracked branch and is never listed).
    depth: int
    parent_id: str | None
    parent: str | None
    #: Position in the depth-first order of all repos' stacks.
    order: int
    #: Checked out in the repo `tt ui` was launched from.
    current: bool
    #: Position in its stack (``stack_pos`` of ``stack_height``): depth + 1,
    #: and the height of the tallest chain in the stack this branch is in.
    stack_pos: int
    stack_height: int
    children: list[ChildRef]
    task: Link | None
    #: True when the configured tracker owns the task, so ``task_live`` is
    #: kept current.
    task_fetchable: bool
    task_live: TaskLive | None
    agent: Link | None
    #: Who holds a claim on it right now (empty: nobody).
    claim: BranchClaim
    #: Where its PR stands in Slack (``None``: Slack is off, or no PR yet).
    slack: SlackInfo | None
    #: Where its PR stands in the merge queue (``None``: nothing to say).
    merge_queue: MergeQueueInfo | None
    #: How its head merges with the other open tasks' (``None``: not worked
    #: out yet, or nothing to say).
    sibling_conflicts: SiblingConflictsInfo | None
    #: ISO time the task started (the branch was registered); ``None`` only
    #: if the database has none for it.
    started_at: str | None
    status: LiveStatus


class FilterOption(Model):
    value: str
    label: str


class FilterSpec(Model):
    key: FilterKey
    label: str
    options: list[FilterOption]


class UntrackedPr(Model):
    """An open PR of the user's that is not a tracked branch: shown as a
    link only (``repo`` is ``owner/name``)."""

    repo: str
    number: int
    title: str
    url: str
    created_at: str
    is_draft: bool


class UntrackedLoading(Model):
    state: Literal["loading"]


class UntrackedLoaded(Model):
    state: Literal["loaded"]
    prs: list[UntrackedPr]
    #: GitHub has more open PRs of the user's than the 100 it was asked for,
    #: so some may be missing.
    truncated: bool
    #: Open PRs GitHub would not show (an organization the token is not
    #: authorized for): they cannot be listed.
    hidden: int


class UntrackedFailed(Model):
    state: Literal["failed"]
    error: str


type UntrackedPrs = Annotated[
    UntrackedLoading | UntrackedLoaded | UntrackedFailed,
    Field(discriminator="state"),
]


class RepoWebhook(Model):
    """A repo the user administers whose GitHub webhook is not working (the
    repo is then only polled)."""

    repo_id: int
    #: The repo's one forwarded webhook is taken: ``claimed``: by a forwarder
    #: toolbelt did not start (connected now); ``leftover``: by a webhook toolbelt
    #: did not create with nothing connected to it; ``blocked``: by something
    #: not known. ``failed``: the forwarder stopped or could not start;
    #: ``not_installed``: the ``gh webhook`` extension is missing.
    state: Literal["claimed", "leftover", "blocked", "failed", "not_installed"]
    #: Why, in a sentence.
    problem: str


class SlackInfo(Model):
    """Where a branch's PR stands in Slack, from the posts toolbelt recorded
    (``None`` on a branch when Slack is not configured or it has no PR)."""

    posted: bool
    #: What the latest post was (``None`` while not posted).
    last_kind: SlackPostKind | None
    #: ISO time of the latest post or bump: what the post age counts from.
    last_at: str | None
    #: Posted a day ago or more, and nobody has looked since.
    bump_due: bool
    #: A message about this PR is being posted in the background.
    posting: bool
    #: What the last post that finished had to say: why it failed (nothing was
    #: recorded), or what Slack's copy was missing. ``None`` when all went well.
    notice: str | None
    #: Link to the Slack thread of the latest post or bump (``None`` when the
    #: config has no ``workspace``).
    thread_link: str | None
    #: Who the latest post or bump asked to review it (``None``: nobody, or not
    #: posted): the card's Assigned Review.
    assignee: str | None


class MergeQueueInfo(Model):
    """Where a branch's PR stands in the merge queue (``None`` on a branch when
    there is nothing to say: not queued, nothing that can be queued, no note)."""

    #: It is approved and waiting on nothing but CI: it can be queued.
    can_queue: bool
    #: Queued: it merges when its CI has passed.
    queued: bool
    #: Its queued merge is running.
    merging: bool
    #: ISO time it was queued.
    queued_at: str | None
    #: Why it left the queue without merging (until it is queued again).
    notice: str | None


type SiblingConflictState = Literal["conflict", "inherited", "unknown"]


class SiblingConflict(Model):
    """Another open task whose branch does not merge cleanly with this one's."""

    branch_id: str
    name: str
    #: ``conflict``: merging the two heads conflicts; ``inherited``: it does,
    #: but only in files their parents already conflict in; ``unknown``: the
    #: other branch's head is not in the local repo (not fetched), so the
    #: merge could not be tried.
    state: SiblingConflictState
    #: The conflicted files, at most ten; ``more`` counts the rest.
    files: list[str]
    more: int


class SiblingConflictsInfo(Model):
    """How a branch's head merges with the other open tasks' of its repo (see
    ``sibling_conflicts``). ``None`` on a branch when there is nothing to say."""

    #: The branch's own head is in the local repo. When it is not (a PR not
    #: fetched), nothing could be tried.
    head_known: bool
    #: Its PR is in review or ready to merge: the card says so loudly; for other
    #: tasks it is a quiet chip.
    prominent: bool
    conflicts: list[SiblingConflict]


class FetchedBranch(Model):
    branch_id: str
    #: ISO time its PR status was last looked up successfully (what the card
    #: shows may come from a lookup saved before a restart); ``None`` when it
    #: never has been.
    fetched_at: str | None
    #: Why the latest lookup failed (the status shown is then the last good
    #: one, as old as ``fetched_at``); ``None`` when it succeeded.
    error: str | None


class Snapshot(Model):
    #: The event version this was read at: events with a higher ``v``
    #: happened after it.
    version: int
    #: Seconds since the last polling pass that fetched anything.
    age: float | None
    repos: list[Repo]
    #: Every tracked branch, repos in name order, each repo's depth-first.
    branches: list[Branch]
    catalog: Catalog
    filters: list[FilterSpec]
    #: Open PRs of the user's that are not tracked branches.
    untracked: UntrackedPrs
    #: When each branch's PR status was last looked up (a branch with no lookup
    #: yet is left out). Cards say how old their data is by it; ``refreshed``
    #: events keep it current.
    fetched: list[FetchedBranch]
    #: What the cards, the Agents view and the Spend view show, as last worked
    #: out (``None``/empty until the server has done it once); events keep them
    #: current.
    extras: CardExtras | None
    agents: Agents | None
    spend: list[Spend]
    #: Repos the user administers whose webhook is not working.
    webhooks: list[RepoWebhook]


# ---- the aggregate view (see aggregate.py) --------------------------------------

#: Who a task in flight can be waiting on (``Who`` without ``none``).
type WaitingOnKey = Literal["you", "agent", "auto", "ci", "others"]


class WaitingOn(Model):
    """The tasks in flight that are waiting on this one, at each sampled
    instant: the "next" lane's phases grouped by who the next action waits
    on."""

    key: WaitingOnKey
    #: The share (0..1) of the tasks in flight; ``None`` where no task was in
    #: flight. Across the keys the shares of an instant sum to 1.
    share: list[float | None]
    #: How many tasks that is.
    count: list[int]


class AggregateSamples(Model):
    """The instants every series of the aggregate view is sampled at: a
    snapshot of the tasks in flight at each (not an average over time)."""

    #: ISO time of each change of a task or lane, plus the window start and now.
    starts: list[str]
    #: How many tasks were in flight at each instant.
    inflight: list[int]


class PrMilestones(Model):
    """One PR of the user's and the UTC instants its lead times are measured
    between. The server does no calendar arithmetic (day, week and weekend
    are the viewer's time zone's): the client buckets and subtracts."""

    #: Lower-case ``owner/name``, and the PR's number, title and URL.
    repo: str
    number: int
    title: str
    url: str
    state: Literal["OPEN", "MERGED", "CLOSED"]
    opened: str
    #: When it was merged or closed; ``None`` while open.
    ended: str | None
    #: When the task began, if recorded, and how it was tied to this PR:
    #: ``id`` (the PR's recorded identity) or ``name`` (the branch name and
    #: the times, for a task that never recorded its PR).
    task_started: str | None
    matched_by: Literal["id", "name"] | None
    first_review_requested: str | None
    #: The first review by someone other than the author, and the first one
    #: at or after the first request.
    first_review: str | None
    first_review_after_request: str | None
    #: Lines the PR adds and deletes against its base (its parent branch for a
    #: stacked PR, so a stack's lines are not counted twice).
    additions: int
    deletions: int


class AggregateCoverage(Model):
    #: Task starts are recorded from this time on (when the stack DB began
    #: archiving removed branches); earlier tasks have no start unless their
    #: branch is still tracked.
    task_starts_since: str
    #: Tasks that have no PR (yet), so are only known from the stack DB.
    tasks_without_pr: int
    #: GitHub had more PRs than were fetched, or some were hidden (an
    #: organization the token is not authorized for).
    truncated: bool
    hidden: int


class AggregateNote(Model):
    label: str
    why: str


class AggregateLanePhase(Model):
    """The share of the tasks in flight (at each of ``mix.starts``) that had
    a span of this kind of lane ``lane`` then, and how many."""

    kind: LanePhaseKind
    share: list[float | None]
    count: list[int]


class AggregateLaneEvents(Model):
    """When (in the window) markers of this kind happened, on the tasks."""

    kind: LaneEventKind
    times: list[str]


class AggregateLane(Model):
    """A lane's layer of the aggregate view: how often its spans were on the
    tasks in flight, and its markers (see ``timeline_lanes.py``)."""

    lane: LaneId
    phases: list[AggregateLanePhase]
    events: list[AggregateLaneEvents]


class ActivityBucket(Model):
    """What agents called: a tool or a category of call, how many times, and
    the call time summed over them (so parallel calls add up past the wall
    time)."""

    name: str
    calls: int
    ms: int


class ActivityHour(Model):
    """The agent turns that started in one hour (UTC): their wall time in the
    parts that add up to it, and what they called. The viewer's time zone makes
    days of these."""

    #: ISO time the hour starts (UTC).
    t: str
    turns: int
    calls: int
    wall_ms: int
    #: Calls, less time blocked on you and waiting on subagents.
    tool_ms: int
    #: Waiting on subagents.
    delegated_ms: int
    #: Permission prompts and dialogs.
    blocked_ms: int
    #: The rest: thinking, streaming, API latency.
    model_ms: int
    tools: list[ActivityBucket]
    categories: list[ActivityBucket]


class AggregateActivity(Model):
    """What agents did in their turns (see ``agent_activity``), recorded by the
    Claude Code plugin: only hours with a turn are listed."""

    hours: list[ActivityHour]
    #: When the first turn was recorded, ``None`` before any: earlier turns are
    #: unknown, not absent.
    since: str | None


class EstimateParams(Model):
    """The estimator's hyperparameters (what backtesting tunes)."""

    half_life_weeks: float
    min_samples: int
    shrinkage: float


class EstimateStageAccuracy(Model):
    """How a backtest's predictions fared for one stage (``all`` for every)."""

    stage: str
    #: Predictions made, and how many were "unknown".
    n: int
    unknown: int
    #: The share of known predictions whose actual time left was at or under
    #: the p80 (the target is 0.8).
    coverage: float | None
    median_abs_error_seconds: float | None
    #: Median of p80 - p50, in working seconds.
    median_width_seconds: float | None


class EstimateRegime(Model):
    """A stage whose median duration moved between two windows."""

    stage: str
    weeks: int
    #: Recent median over the prior one, minus one (-0.4: 40% shorter).
    change: float
    recent_median_seconds: float
    prior_median_seconds: float
    n_recent: int
    n_prior: int


class EstimateBacktest(Model):
    ran_at: str
    params: EstimateParams
    overall: EstimateStageAccuracy
    by_stage: list[EstimateStageAccuracy]
    regime: list[EstimateRegime]


class EstimateAccuracy(Model):
    """The finish estimate's track record: the stored backtests, oldest
    first, and the shown estimates compared with the actual finishes."""

    #: The length of one working day, to show working seconds as days.
    working_day_seconds: float
    #: The parameters the live estimates use now (``None``: the defaults, no
    #: backtest yet).
    current: EstimateParams | None
    backtests: list[EstimateBacktest]
    #: Estimates shown so far, those whose task has finished since, and the
    #: share of those finished at or before their p80 instant.
    live_made: int
    live_finished: int
    live_coverage: float | None


class Aggregate(Model):
    weeks: int
    window_start: str
    window_end: str
    #: When the cached GitHub history was last refreshed, and how many PRs
    #: were queried in that refresh.
    fetched_at: str
    refreshed: int
    #: The repo this is narrowed to (its name), ``None`` for all of them.
    #: Sample sizes shrink per repo: ``coverage`` says how many PRs.
    repo: str | None
    #: The PRs of (a little more than) the window, with their milestones.
    prs: list[PrMilestones]
    samples: AggregateSamples
    #: Who the tasks' Next waits on (the "next" lane, grouped).
    waiting_on: list[WaitingOn]
    #: Agent turns: time by kind and what they called (not status: activity).
    activity: AggregateActivity
    #: One layer per lane the aggregate counts (``timeline_lanes.LANE_SOURCES``
    #: without an ``aggregate_skip``), sampled at ``mix.starts``.
    lanes: list[AggregateLane]
    coverage: AggregateCoverage
    #: What could not be reconstructed or is approximated.
    notes: list[AggregateNote]
    #: The finish estimate's track record (``None`` where not computed).
    estimate: EstimateAccuracy | None = None


# ---- spend ------------------------------------------------------------------------


class SpendTokens(Model):
    """Tokens by kind; ``input`` is the uncached input."""

    input: int
    output: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int


class SpendWindow(Model):
    label: str
    #: ISO time the window began.
    start: str
    calls: int
    tokens: SpendTokens
    #: Cost of the priced calls only.
    cost_usd: float
    unpriced_calls: int


class SpendModel(Model):
    model: str
    calls: int
    tokens: SpendTokens
    cost_usd: float
    unpriced_calls: int


class SpendTask(Model):
    """Spend attributed to one task through claims. ``task_id`` is ``None``
    for the spend no claim covers; ``repo`` and ``branch`` are ``None`` there
    and for a task that is no longer known. A call two tasks' claims cover at
    once is split evenly, so the figures are fractional."""

    task_id: str | None
    repo: str | None
    branch: str | None
    calls: float
    tokens: float
    cost_usd: float
    unpriced_calls: float


class UnpricedModel(Model):
    model: str
    calls: int


class AutonomousWindow(Model):
    label: str
    start: str
    runs: int
    cost_usd: float
    tokens: SpendTokens


class AutonomousTrigger(Model):
    trigger: AgentTrigger
    runs: int
    cost_usd: float


class AutonomousTask(Model):
    task_id: str
    repo: str | None
    branch: str
    runs: int
    cost_usd: float


class AutonomousSpend(Model):
    """What autonomous agent runs cost: exactly, from each run's own record
    (not estimated from transcripts like the interactive figures)."""

    windows: list[AutonomousWindow]
    by_trigger: list[AutonomousTrigger]
    by_task: list[AutonomousTask]


class SlackWindow(Model):
    label: str
    start: str
    sessions: int
    cost_usd: float


class SlackPurpose(Model):
    purpose: Literal["post", "bump", "reassign", "import"]
    sessions: int
    cost_usd: float


class SlackSpend(Model):
    """What the sessions that read or posted in Slack for toolbelt cost:
    exactly, from each session's own report. Not in the interactive figures."""

    windows: list[SlackWindow]
    by_purpose: list[SlackPurpose]


class SpendBucket(Model):
    """What one stretch of time cost."""

    #: ISO time the bucket starts (UTC).
    start: str
    #: Interactive sessions, estimated from transcripts.
    interactive_usd: float
    #: Autonomous runs that started in it, by what they were for: each run's
    #: own reported cost.
    resolve_conflicts_usd: float
    resolve_comments_usd: float
    fix_ci_usd: float


class SpendOverTime(Model):
    #: How long each bucket is, in hours (an hour for a one-day window, else a day).
    bucket_hours: int
    buckets: list[SpendBucket]


class Spend(Model):
    as_of: str
    #: When the transcripts were last read; ``None`` until the first read ends.
    ingested_at: str | None
    #: The window the by-model and by-task tables cover.
    breakdown_days: int
    windows: list[SpendWindow]
    by_model: list[SpendModel]
    by_task: list[SpendTask]
    #: Models with calls that have no price, so their cost is missing.
    unpriced_models: list[UnpricedModel]
    #: When claims began to be recorded (``None``: none yet).
    claims_since: str | None
    autonomous: AutonomousSpend
    slack: SlackSpend
    over_time: SpendOverTime


# ---- autonomous agents ------------------------------------------------------------


type AgentReasonCode = Literal[
    "disabled",
    "pr_not_open",
    "has_pr",
    "working",
    "deciding",
    "draft",
    "inactive",
    "claimed",
    "no_worktree",
    "worktree_dirty",
    "worktree_busy",
    "thrash",
    "runs_per_head",
    "task_runs_per_day",
    "task_usd_per_day",
    "global_usd_per_day",
    "global_usd_per_month",
    "auth",
    "screen_asleep",
    "screen_locked",
    "backoff",
]

type AgentTrigger = Literal[
    "resolve_conflicts", "resolve_comments", "fix_ci", "validate"
]
type ValidateResult = Literal["pass", "fail"]

type AgentRunState = Literal[
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cap_reached",
    "stopped",
    "abandoned",
    "deciding",
]


class AgentReason(Model):
    """Why an agent is not (or not yet) on a task, or on any task."""

    code: AgentReasonCode
    text: str
    #: ISO time it stops being true by itself, when it does.
    until: str | None


class AgentDecision(Model):
    """What the dispatcher concluded about one task whose next action is
    something an agent does. A reason of ``working`` means an agent is on it."""

    #: The branch's row id (see ``Branch.id``).
    id: str
    repo: str
    branch: str
    trigger: AgentTrigger
    #: ``None``: an agent may start on it now.
    reason: AgentReason | None
    decided_at: str
    #: A person can start a run by hand past ``reason``.
    can_override: bool
    #: A run was asked for by hand and has not started yet.
    starting: bool
    #: Why a run asked for by hand did not start (``None``: it did, or none
    #: was asked for).
    start_error: str | None


class AgentRunInfo(Model):
    run_id: str
    #: The branch's row id; the branch may be gone since.
    id: str
    repo: str
    branch: str
    trigger: AgentTrigger
    model: str
    state: AgentRunState
    started_at: str
    ended_at: str | None
    turns: int
    cost_usd: float
    #: The agent's own report of what it did.
    summary: str | None
    error: str | None
    #: What a working run is doing now, in a few words (``None``: nothing yet,
    #: or the run ended). Its turns and cost so far are in ``turns`` and
    #: ``cost_usd``.
    activity: str | None
    #: A validate run's verdict on the branch (``None``: not a validate run, or
    #: it reported none).
    result: ValidateResult | None


class AgentLimitsInfo(Model):
    model: str
    run_usd: float
    run_turns: int
    run_timeout_seconds: int
    task_runs_per_head: int
    task_runs_per_day: int
    task_usd_per_day: float
    global_usd_per_day: float
    global_usd_per_month: float
    active_working_days: int


class AgentSpend(Model):
    """What agents have spent, against the global limits."""

    last_24_hours_usd: float
    this_month_usd: float


class AgentScreen(Model):
    awake: bool
    locked: bool


class Agents(Model):
    enabled: bool
    #: The switch for starting runs (off: a dry run).
    launching: bool
    #: Nothing launches runs yet: tasks are decided but none is started.
    dry_run: bool
    #: When the dispatcher last decided (``None``: not yet).
    ticked_at: str | None
    #: Why its last pass failed (``None``: it did not).
    error: str | None
    #: What stops every agent right now.
    blockers: list[AgentReason]
    limits: AgentLimitsInfo
    spend: AgentSpend
    #: ``None`` when the screen could not be read.
    screen: AgentScreen | None
    #: ``None`` until checked (it is not while agents are off).
    signed_in: bool | None
    decisions: list[AgentDecision]
    #: The most recent runs, newest first.
    runs: list[AgentRunInfo]


class CardAgent(Model):
    """What a task card says about the autonomous agents on one branch."""

    #: The branch's next action is something an agent does (``trigger`` says
    #: what); without it there is nothing for an agent to take.
    has_action: bool
    trigger: AgentTrigger | None
    #: Why no agent is on the task (``working``: one is); ``None`` with an
    #: action: one may start on it.
    reason: AgentReason | None
    #: A person can start a run by hand past ``reason``.
    can_override: bool
    #: A run was asked for by hand and has not started yet.
    starting: bool
    #: Why a run asked for by hand did not start.
    start_error: str | None
    #: The branch's most recent run, if it has had one.
    latest_run: AgentRunInfo | None
    runs: int
    #: The branch's runs, newest first (``latest_run`` is the first), at most
    #: ``card_extras.RECENT_RUNS``.
    recent_runs: list[AgentRunInfo]
    validation: CardValidation


class CardValidation(Model):
    """Where a task stands on "validate, personal review, publish"
    (``docs/personal-review.md``)."""

    #: The task has no PR, so a validate run can be started on it by hand.
    can_validate: bool
    #: A validate run ended and nothing since sent the task back: the task
    #: reads Personal review.
    validated: bool
    #: The latest validate run's verdict.
    result: ValidateResult | None
    #: The PR is being opened after an approval.
    opening_pr: bool
    #: Why the PR could not be opened after an approval.
    notice: str | None


class CardSpend(Model):
    """What a task has cost, over all the spend on record."""

    #: Interactive sessions, estimated from their transcripts at list prices.
    interactive_usd: float
    #: Calls with no price (so ``interactive_usd`` is low by their cost).
    interactive_unpriced_calls: float
    #: Autonomous runs, as each run's own reported cost.
    autonomous_usd: float
    autonomous_runs: int


class LaneEvent(Model):
    t: str
    kind: LaneEventKind
    #: What the tooltip adds (who, which run, which claim).
    detail: str


class LanePhase(Model):
    start: str
    #: ``None``: still going (the track draws it to now).
    end: str | None
    kind: LanePhaseKind
    detail: str
    #: The lane's own sub-status of the span (a claim: its holder's state;
    #: ``""``: the lane has none).
    state: str = ""


class TimelineLane(Model):
    """One lane of a card's timeline, under the PR track (see
    ``timeline_lanes.py``): what that source recorded for the branch."""

    lane: LaneId
    events: list[LaneEvent]
    phases: list[LanePhase]


class CardEstimate(Model):
    """When a task is estimated to be finished (merged): a median and a p80
    as UTC instants, or unknown. Working-time arithmetic is the server's (the
    working hours are in its config); the client formats the instants."""

    #: ``False``: not enough history; no instant is given.
    known: bool
    #: The stage the task is in (a kind of its Next).
    stage: str
    finish_p50: str | None
    finish_p80: str | None
    #: The stage has lasted longer than any in the history.
    overdue: bool
    #: The stages after this one are not in the estimate.
    partial: bool
    #: Which fallback answered (``reviewer+size``, ``stage``, ``global``, ...).
    level: str
    #: The history samples it is based on.
    n: int
    explanation: str


class BranchExtras(Model):
    """The parts of a task card that do not come with its PR status."""

    #: The branch's row id (see ``Branch.id``).
    id: str
    #: The lanes with something to draw, in the catalog's lane order.
    lanes: list[TimelineLane]
    agent: CardAgent
    spend: CardSpend
    #: When the task is estimated to finish (``None``: no PR to estimate).
    estimate: CardEstimate | None = None


class CardExtras(Model):
    branches: list[BranchExtras]
    #: When the autonomous agents last decided every task (``None``: not yet).
    agents_decided_at: str | None
    #: When the session transcripts were last read for spend (``None``: not yet).
    spend_read_at: str | None


class MergeRequest(Model):
    """The body of ``POST /api/merge``: land the branch's PR."""

    #: The branch's row id (see ``Branch.id``).
    id: str


class MergeQueueRequest(Model):
    """The body of ``POST /api/merge/queue`` and ``/api/merge/unqueue``."""

    #: The branch's row id (see ``Branch.id``).
    id: str


class RefreshRequest(Model):
    """The body of ``POST /api/refresh``: look the branch's PR up again now."""

    #: The branch's row id (see ``Branch.id``).
    id: str


class RefreshAccepted(Model):
    """The answer to ``POST /api/refresh``: the branch is being looked up again
    in the background (its card's age moves when the lookup arrives)."""

    refreshing: bool


class DecisionOptionInfo(Model):
    id: str
    title: str
    #: What picking it means: always shown.
    summary: str
    #: More context, shown on request.
    context: str
    benefits: str
    costs: str
    risks: str


class DecisionRecommendationInfo(Model):
    option_id: str | None
    reasoning: str


class DecisionPointInfo(Model):
    id: str
    title: str
    context: str
    options: list[DecisionOptionInfo]
    recommendation: DecisionRecommendationInfo | None


class DecisionAnswerInfo(Model):
    point_id: str
    option_id: str | None
    text: str


class DecisionInfo(Model):
    """A question an agent asked about a task, and the user's answer once given."""

    id: str
    branch: str
    #: ``interactive`` or ``autonomous``.
    proposer: Literal["interactive", "autonomous"]
    session_id: str | None
    title: str
    points: list[DecisionPointInfo]
    status: DecisionStatus
    answers: list[DecisionAnswerInfo]
    created_at: str
    answered_at: str | None
    #: When the answer reached the agent (``None``: not yet, or not answered).
    delivered_at: str | None


class DecisionList(Model):
    """The decisions about one branch's task, oldest first."""

    decisions: list[DecisionInfo]


class DecisionAnswerRequest(Model):
    """The body of ``POST /api/decisions/answer``: one answer per point, each an
    option, words, or both."""

    decision_id: str
    answers: list[DecisionAnswerInfo]


class DecisionDeleteRequest(Model):
    decision_id: str


class SlackPerson(Model):
    """Someone who can be asked to review: Slack display name and user ID."""

    id: str
    name: str


class SlackPrRef(Model):
    """A PR named in a Slack message: ``repo`` is ``owner/name``, lower case."""

    repo: str
    number: int
    #: Who is asked about this PR, when not the message's ``assignee_id`` (a post
    #: of several PRs can ask a different person about each).
    assignee_id: str | None = None


class SlackDraftPr(Model):
    repo: str
    number: int
    url: str
    title: str
    additions: int
    deletions: int
    #: Who the draft asks about this PR.
    assignee: SlackPerson | None
    #: The message is about this PR. A post lists every PR of the stack that
    #: could be in it; one that is not ``included`` is shown unchecked (an
    #: agent holds its task: ``next`` says so).
    included: bool
    #: Where the PR stands, for the dialog to show beside its checkbox.
    pr: PrState
    ci: CiState
    review: ReviewState
    open_comments: int
    has_conflicts: bool
    #: The branch's next action: "With an agent" or "Autonomous agent" (with
    #: ``would_be`` the action it replaced) when an agent holds the task.
    #: ``None`` while its status is not loaded.
    next: NextActionInfo | None


class SlackDraftRequest(Model):
    """The body of ``POST /api/slack/draft``: the message to post or bump the
    branch's PR, as toolbelt would write it."""

    #: The branch's row id (see ``Branch.id``).
    id: str
    #: Ask this person instead of the one the rules chose (``None``: the rules).
    assignee_id: str | None
    #: A post of a stack: the PRs to include, each with who is asked about it
    #: when not ``assignee_id``. ``None``: the PRs that are ready (the draft
    #: lists the others, unchecked).
    prs: list[SlackPrRef] | None = None
    #: Ask someone else to review the branch's PR, in the thread it was posted
    #: in: allowed whenever it has been posted, not only once a bump is due.
    reassign: bool = False


class SlackDraft(Model):
    """A message waiting for the user to confirm. The text shows a person as
    ``@name``; it becomes a real Slack mention when sent."""

    kind: Literal["post", "bump", "reassign"]
    #: Where it will be posted (from the config, never from the client).
    channel: str
    text: str
    assignee: SlackPerson | None
    #: Who else can be asked.
    candidates: list[SlackPerson]
    #: The PRs the message is about.
    prs: list[SlackDraftPr]
    #: A bump is a reply under this message (``None`` for a post).
    thread_ts: str | None
    #: Link to that message in Slack (``None`` for a post, or when the config
    #: has no ``workspace``).
    thread_link: str | None


class SlackSendRequest(Model):
    """The body of ``POST /api/slack/send``: post the confirmed (possibly
    edited) message and record it."""

    id: str
    kind: Literal["post", "bump", "reassign"]
    text: str
    assignee_id: str | None
    thread_ts: str | None
    prs: list[SlackPrRef]


class SlackAccepted(Model):
    """The answer to ``POST /api/slack/send``: the message is being posted in
    the background. Its PRs read ``posting`` until it is done; then they read
    posted, or carry a ``notice`` saying why not."""

    prs: list[SlackPrRef]


class AgentsEnabled(Model):
    """The body of ``POST /api/agents/enabled``."""

    enabled: bool


class AgentStart(Model):
    """The body of ``POST /api/agents/start``: start a run by hand."""

    #: The branch's row id (see ``AgentDecision.id``).
    id: str


class AgentMessage(Model):
    """One step of a run's conversation."""

    kind: Literal["prompt", "text", "tool_use", "tool_result"]
    at: str
    text: str
    #: The tool called (``tool_use``).
    tool: str | None
    #: The tool reported an error (``tool_result``).
    is_error: bool


class AgentConversation(Model):
    """What a run has said and done so far."""

    run_id: str
    #: The run is still working: more may come.
    running: bool
    messages: list[AgentMessage]


class AgentsLaunching(Model):
    """The body of ``POST /api/agents/launching``."""

    launching: bool


# ---- server-sent events ---------------------------------------------------------


class HelloEvent(Model):
    """Sent on connect: the current version (a client that last saw another
    one missed something) and how stale the cache is."""

    type: Literal["hello"]
    v: int
    age: float | None
    #: Which build of the page the server serves now (``None``: none built). A
    #: page that runs another one has been left open across an update.
    build: str | None


class StatusEvent(Model):
    """A branch's status changed (also sent for every branch stacked on it:
    its next action depends on this one's PR)."""

    type: Literal["status"]
    v: int
    branch_id: str
    status: LiveStatus
    #: Its PR's standing in Slack, which changes with the PR (a review means
    #: no bump) and with time (a bump falls due): ``None`` when Slack is off.
    slack: SlackInfo | None
    #: Its PR's standing in the merge queue, which changes with its CI and
    #: reviews: ``None`` when there is nothing to say.
    merge_queue: MergeQueueInfo | None
    #: Its merge conflicts with the other open tasks, which change with their
    #: heads.
    sibling_conflicts: SiblingConflictsInfo | None


class TaskEvent(Model):
    type: Literal["task"]
    v: int
    branch_id: str
    task: TaskLive


class StructureEvent(Model):
    """The stack DB was written: the client re-fetches the snapshot."""

    type: Literal["structure"]
    v: int


class RefreshedEvent(Model):
    """A polling pass finished: the branches it looked up (changed or not)
    and when, which their cards say they are as old as."""

    type: Literal["refreshed"]
    v: int
    fetched: list[FetchedBranch]


class ExtrasEvent(Model):
    """What the task cards show beyond their PR status changed."""

    type: Literal["extras"]
    v: int
    extras: CardExtras


class WebhooksEvent(Model):
    """Which administered repos have a webhook that is not working changed."""

    type: Literal["webhooks"]
    v: int
    webhooks: list[RepoWebhook]


class AgentsEvent(Model):
    """The autonomous agents' state changed (what the Agents view shows)."""

    type: Literal["agents"]
    v: int
    agents: Agents


class SpendEvent(Model):
    """The spend ledger was read again: the Spend view's data for each of its
    windows (a one-, seven- and thirty-day breakdown)."""

    type: Literal["spend"]
    v: int
    spend: list[Spend]


class ConversationEvent(Model):
    """A run that is working said or did something: its conversation so far."""

    type: Literal["conversation"]
    v: int
    conversation: AgentConversation


class UntrackedEvent(Model):
    """The list of open PRs that toolbelt does not track changed."""

    type: Literal["untracked"]
    v: int
    untracked: UntrackedPrs


class ClaimPatch(Model):
    branch_id: str
    claim: BranchClaim
    #: Re-derived with the claim: whether it is "With an agent" changes the
    #: next action.
    status: LiveStatus


class ClaimsEvent(Model):
    """Claims changed on some branches. Lists those and the branches stacked
    on them (their next action follows the parent's)."""

    type: Literal["claims"]
    v: int
    branches: list[ClaimPatch]


type ServerEvent = Annotated[
    HelloEvent
    | StatusEvent
    | TaskEvent
    | ClaimsEvent
    | StructureEvent
    | RefreshedEvent
    | UntrackedEvent
    | ExtrasEvent
    | WebhooksEvent
    | AgentsEvent
    | SpendEvent
    | ConversationEvent,
    Field(discriminator="type"),
]


# ``Snapshot`` names models defined after it.
Snapshot.model_rebuild()


# --- Diff review ------------------------------------------------------------


class FileReviewInfo(Model):
    """A changed file of a branch awaiting its personal review, and where your
    review of it stands."""

    path: str
    #: Where a renamed file was (``None``: it was not renamed).
    old_path: str | None
    status: Literal["A", "M", "D", "R"]
    #: The hash of this file's own diff against the parent: what a decision is
    #: for, and what a decision sent back must name.
    diff_hash: str
    state: Literal["unreviewed", "approved", "changes_requested"]
    #: The note of the decision (``""``: none).
    note: str


class PersonalReviewState(Model):
    """What the Review view needs to know of the branch it shows, when it is a
    tracked branch with no PR that a validate run has reviewed."""

    #: The branch's row id (see ``Branch.id``).
    id: str
    files: list[FileReviewInfo]
    result: ValidateResult | None
    summary: str | None
    opening_pr: bool
    notice: str | None
    #: Slack is configured, so an approval can post the PR.
    can_post_in_slack: bool


class FileApprovalRequest(Model):
    """The body of ``POST /api/review/file``: approve one file's diff, or
    request changes to it with a note."""

    id: str
    path: str
    #: The hash of the file's diff the user reviewed (a changed diff refuses it).
    diff_hash: str
    state: FileApprovalState
    note: str
    #: Approving the last unapproved file opens the PR; post it in Slack as soon
    #: as it is open (``False``: it reads Post in Slack, as for any ready PR).
    post_in_slack: bool = False


class ApproveAllRequest(Model):
    """The body of ``POST /api/review/approve-all``: approve every file's diff
    as the page showed it."""

    id: str
    #: Each file's path and the hash of its diff on the page (a different diff
    #: refuses it).
    files: dict[str, str]
    note: str
    post_in_slack: bool = False


class ReviewChangedFile(Model):
    #: Added, modified, deleted or renamed.
    status: Literal["A", "M", "D", "R"]
    path: str
    #: Where a renamed file was (``None``: it was not renamed).
    old_path: str | None


class DifftasticModel(Model):
    """difftastic's JSON output: its format is not stable, so keys this client
    does not read are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class DifftasticSpan(DifftasticModel):
    #: Columns in UTF-8 bytes from the start of the line.
    start: int
    end: int
    content: str
    highlight: str


class DifftasticSide(DifftasticModel):
    #: 0-indexed.
    line_number: int
    changes: list[DifftasticSpan]


class DifftasticLinePair(DifftasticModel):
    lhs: DifftasticSide | None = None
    rhs: DifftasticSide | None = None


class DifftasticFile(DifftasticModel):
    language: str
    path: str
    status: Literal["changed", "created", "deleted", "unchanged"]
    #: The line pairs that line up the two sides (``None`` for a created,
    #: deleted or unchanged file).
    aligned_lines: list[tuple[int | None, int | None]] | None = None
    #: The changed line pairs, in groups (``None`` as ``aligned_lines``).
    chunks: list[list[DifftasticLinePair]] | None = None


class ReviewFile(Model):
    path: str
    old_path: str | None
    status: Literal["A", "M", "D", "R"]
    old_text: str
    new_text: str
    difftastic: DifftasticFile
    #: The language id of the configured language server that answers for this
    #: file (``None``: none is configured for its extension, so no hover).
    language_server: str | None


class ReviewHover(Model):
    #: ``None``: the language server has nothing at that position.
    text: str | None


class ReviewDefinitionTarget(Model):
    #: Relative to the revision's checkout; ``None`` when the target is
    #: outside it (the standard library, installed packages).
    path: str | None
    uri: str
    #: 0-indexed; the character is in UTF-16 code units.
    line: int
    character: int


class ReviewDefinition(Model):
    #: ``None``: the language server found no definition.
    target: ReviewDefinitionTarget | None


class Wire(Model):
    """The schema root: lists everything the client reads (never
    instantiated)."""

    snapshot: Snapshot
    event: ServerEvent
    aggregate: Aggregate
    spend: Spend
    agents: Agents
    agents_enabled: AgentsEnabled
    agents_launching: AgentsLaunching
    merge_request: MergeRequest
    merge_queue_request: MergeQueueRequest
    review_files: list[ReviewChangedFile]
    review_file: ReviewFile
    review_hover: ReviewHover
    review_definition: ReviewDefinition
    personal_review_state: PersonalReviewState
    file_approval_request: FileApprovalRequest
    approve_all_request: ApproveAllRequest
    merge_queue_info: MergeQueueInfo
    slack_draft_request: SlackDraftRequest
    slack_draft: SlackDraft
    slack_send_request: SlackSendRequest
    slack_accepted: SlackAccepted
    decision_list: DecisionList
    decision_info: DecisionInfo
    decision_answer_request: DecisionAnswerRequest
    decision_delete_request: DecisionDeleteRequest
    agent_conversation: AgentConversation
    agent_start: AgentStart
