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

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from toolbelt.git.stack.status import CiState, PrState, ReviewState
from toolbelt.task_claims.claim_file import HolderKind, HolderState
from toolbelt.ui import pr_states
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


class Catalog(Model):
    events: EventMarkers  # type: ignore[valid-type]
    phases: PhaseSpans  # type: ignore[valid-type]
    chips: ChipGroups
    slots: SlotDimensions  # type: ignore[valid-type]
    states: list[CatalogState]
    not_represented: list[NotRepresented]


def build_catalog() -> Catalog:
    """The catalog of ``pr_states``, validated into the wire model (so a
    value the types do not know about is an error here, not a blank on the
    page)."""
    return Catalog.model_validate(pr_states.catalog())


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
    #: Reviewers with a review request outstanding.
    reviewers: list[str]
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


class FetchedBranch(Model):
    branch_id: str
    #: ISO time its PR status was looked up.
    fetched_at: str


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


# ---- the aggregate view (see aggregate.py) --------------------------------------

#: The states a task can be in on the status-mix chart, in stacking order:
#: the Next rules (``next_action.py``) that can be told apart from history.
type MixState = Literal[
    "working",
    "draft",
    "ready",
    "waiting",
    "changes",
    "ci_running",
    "ci_failing",
    "approved",
    "blocked",
]
#: The coarse classes the Next rules fall into (``Who`` without ``none``).
type MixWho = Literal["you", "ci", "others"]


class MixStateInfo(Model):
    """One band of the status mix: its label, tone and legend come from the
    state catalog (``pr_states``), so it reads like the cards."""

    key: MixState
    label: str
    tone: Tone
    legend: str


class MixSeries(Model):
    key: MixState
    #: The share (0..1) of the tasks in flight at each sampled instant that
    #: are in this state; ``None`` where no task was in flight. Across the
    #: states the shares of an instant sum to 1.
    share: list[float | None]
    #: How many tasks that is.
    count: list[int]


class MixWhoSeries(Model):
    key: MixWho
    share: list[float | None]
    count: list[int]


class StatusMix(Model):
    """A snapshot of the tasks in flight at each transition instant (not an
    average over time)."""

    #: Zero for event-driven instants.
    step_hours: int
    #: ISO time of each task transition, plus the window start and now.
    starts: list[str]
    #: How many tasks were in flight at each instant.
    inflight: list[int]
    states: list[MixStateInfo]
    series: list[MixSeries]
    who: list[MixWhoSeries]


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
    mix: StatusMix
    coverage: AggregateCoverage
    #: What could not be reconstructed or is approximated.
    notes: list[AggregateNote]


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
    over_time: SpendOverTime


# ---- autonomous agents ------------------------------------------------------------


type AgentReasonCode = Literal[
    "disabled",
    "pr_not_open",
    "working",
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
    "concurrency",
    "backoff",
]

type AgentTrigger = Literal["resolve_conflicts", "resolve_comments", "fix_ci"]

type AgentRunState = Literal[
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cap_reached",
    "stopped",
    "abandoned",
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
    #: The branch's most recent run, if it has had one.
    latest_run: AgentRunInfo | None
    runs: int


class CardSpend(Model):
    """What a task has cost, over all the spend on record."""

    #: Interactive sessions, estimated from their transcripts at list prices.
    interactive_usd: float
    #: Calls with no price (so ``interactive_usd`` is low by their cost).
    interactive_unpriced_calls: float
    #: Autonomous runs, as each run's own reported cost.
    autonomous_usd: float
    autonomous_runs: int


class BranchExtras(Model):
    """The parts of a task card that do not come with its PR status."""

    #: The branch's row id (see ``Branch.id``).
    id: str
    agent: CardAgent
    spend: CardSpend


class CardExtras(Model):
    branches: list[BranchExtras]


class MergeRequest(Model):
    """The body of ``POST /api/merge``: land the branch's PR."""

    #: The branch's row id (see ``Branch.id``).
    id: str


class MergeResult(Model):
    """What `toolbelt git merge` did."""

    ok: bool
    #: The end of its output (the error when it failed).
    output: str


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


class StatusEvent(Model):
    """A branch's status changed (also sent for every branch stacked on it:
    its next action depends on this one's PR)."""

    type: Literal["status"]
    v: int
    branch_id: str
    status: LiveStatus


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
    | UntrackedEvent,
    Field(discriminator="type"),
]


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
    card_extras: CardExtras
    merge_request: MergeRequest
    merge_result: MergeResult
    agent_conversation: AgentConversation
    agent_start: AgentStart
