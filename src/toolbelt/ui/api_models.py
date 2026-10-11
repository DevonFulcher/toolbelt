"""The wire types of `tt ui`'s JSON API: the one definition of what the server
sends and what the web client (``web/``) reads.

Two things are served, both made of the models below:

* ``GET /api/snapshot``: ``Snapshot``, every repo's branches (stack structure,
  links, live status) plus the state catalog;
* ``GET /api/events``: a Server-Sent Events stream of ``ServerEvent``s that
  keep a snapshot current (a branch's new status, a task's title, a structure
  change, ...).

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


class PrDetail(Model):
    title: str
    pr_age: Age
    draft_age: Age | None
    review_request_age: Age | None
    ci_failures: int
    #: Reviewers with a review request outstanding.
    reviewers: list[str]
    #: ISO time the PR was created, when it ended (merged/closed; ``None``
    #: while open) and whether it is a draft: what the per-card timeline
    #: track is scaled and shaded from.
    created: str
    ended: str | None
    is_draft: bool
    events: list[TimelineEvent]


class NextActionInfo(Model):
    label: str
    #: Who is blocking progress.
    who: Who
    #: Which rule fired (see ``next_action.py``).
    kind: NextKind


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
    next: NextActionInfo
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
    """A polling pass finished (drives the "updated Ns ago" label)."""

    type: Literal["refreshed"]
    v: int


class UntrackedEvent(Model):
    """The list of open PRs that toolbelt does not track changed."""

    type: Literal["untracked"]
    v: int
    untracked: UntrackedPrs


type ServerEvent = Annotated[
    HelloEvent
    | StatusEvent
    | TaskEvent
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
