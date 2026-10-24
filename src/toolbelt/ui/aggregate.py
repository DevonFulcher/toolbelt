"""How work has flowed: the aggregate view's numbers.

Pure functions over what was fetched (``PrRecord``: the user's PRs of a
window with their timelines; ``TaskFact``: when each branch's task started,
from the stack DB's tracked and archived rows) and a clock. Nothing here does
I/O; ``aggregate_feed.py`` fetches and caches the inputs.

**What a task is.** A PR, plus the working time before it when the task's
start is known; or, for a branch that never got a PR, only the stack DB row.
Time in flight runs from the task's start (else the PR's creation) until the
PR merged or closed (a reopened PR counts again from the reopening).

**States.** Each task, at each moment of its life, is in one of the states of
``STATES``: the Next rules of ``next_action.py`` that history can tell apart,
checked in the same priority order (changes requested, CI failing, CI
running, draft, approved/blocked, review requested, ready without a
reviewer). The label, tone and legend of each come from the state catalog
(``pr_states``), so the charts read like the cards. The draft, waiting,
approved and CI-failing spans follow the card timeline's phase rules exactly
(``phase_spans`` mirrors ``phaseSpans`` in ``web/src/logic/timeline.ts``; one
shared set of vectors is checked against both).

**Claims and lanes.** The time a claim was held on a task (``claim_spans``,
via the claim lane of ``lane_history``) is the "agent" state: it replaces the
PR's own state for that time, as the claimed Next action does. Every other
lane that the aggregate counts (``timeline_lanes.LANE_SOURCES`` without an
``aggregate_skip``) is a layer in ``Aggregate.lanes``: at each sampled instant,
how many of the tasks in flight had a span of each of its kinds, and when its
markers happened. A lane added to the registry gets a layer, or says why not.

**What history cannot give.** Merge conflicts and unresolved review threads
are only known now (``pr_states.NOT_REPRESENTED``) apart from what ``tt ui``
recorded, so "Resolve conflicts" / "Resolve comments" never appear in the mix;
time spent there is shown as whatever else applies. CI follows ``pr_activity.commit_ci_phases``, as the card does:
"running" is the time from a push until its check suites completed (their
``updatedAt``) whatever the outcome, or until now for a still-pending head
commit; "failing" is the time from that completion with a failing result
until the next push. A PR whose timeline is longer than the fetched 100 items is
truncated at its newest end.
"""

import bisect
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from toolbelt.ui import api_models as api
from toolbelt.ui import pr_activity as act
from toolbelt.ui import pr_states
from toolbelt.ui.pr_activity import PrFacts, TimelineItem
from toolbelt.ui.timeline_lanes import LANE_SOURCES

WINDOWS = (4, 12, 26, 52)
DEFAULT_WEEKS = 12

WHO_YOU = "you"
WHO_AGENT = "agent"
WHO_CI = "ci"
WHO_OTHERS = "others"
WHOS = (WHO_YOU, WHO_AGENT, WHO_CI, WHO_OTHERS)


@dataclass(frozen=True)
class StateDef:
    key: str
    #: The catalog entry the label/tone/legend come from: ``("phase", key)``
    #: or ``("state", "<dimension>:<value>")``.
    source: tuple[str, str]
    #: Who it waits on; ``None`` for "blocked", which follows the parent.
    who: str | None


#: Stacking order, bottom to top: early in a task's life to late.
STATES: tuple[StateDef, ...] = (
    StateDef("working", ("phase", "working"), WHO_YOU),
    StateDef("draft", ("phase", "draft"), WHO_YOU),
    StateDef("ready", ("state", "next:request_review"), WHO_YOU),
    StateDef("waiting", ("phase", "waiting"), WHO_OTHERS),
    StateDef("changes", ("state", "review:reviewed"), WHO_YOU),
    StateDef("ci_running", ("phase", "ci_running"), WHO_CI),
    StateDef("ci_failing", ("phase", "ci_failing"), WHO_YOU),
    StateDef("approved", ("phase", "approved"), WHO_YOU),
    StateDef("blocked", ("state", "next:blocked"), None),
    StateDef("agent", ("state", "next:claimed"), WHO_AGENT),
)
_STATE_BY_KEY = {state.key: state for state in STATES}

NOTES: tuple[tuple[str, str], ...] = (
    (
        "Merge conflicts and unresolved threads",
        (
            "GitHub keeps no history of either, so 'Resolve conflicts' and "
            "'Resolve comments' are not separated in the status mix: that time "
            "is counted as whatever else applies. The conflicts lane layer shows "
            "the conflicts `tt ui` saw."
        ),
    ),
    (
        "Waiting on CI",
        (
            "From a push until that commit's check suites completed, whatever "
            "the outcome (CI takes time to run even when it fails), or until now "
            "while they are pending. 'CI failing' then runs from the completion "
            "until the next push. A failed commit with no known completion time "
            "counts as failing from its push, so its running time is not "
            "separated."
        ),
    ),
    (
        "Tasks before their PR",
        (
            "Only tasks whose start was recorded (a branch created with toolbelt) "
            "have a working span; others begin when their PR opened. A start is "
            "tied to its PR by the recorded PR id; only a task that never "
            "recorded its PR falls back to the branch name."
        ),
    ),
    (
        "Blocked on a parent",
        (
            "Rebuilt from each PR's base branch over time and the parent PR's "
            "state, with the same rule as Next: it waits on you when the parent "
            "does."
        ),
    ),
    (
        "Agent claims and lane layers",
        (
            "'With an agent' is the time a claim was held on the task (an agent "
            "session, a person or an autonomous agent), from the claim record "
            "`tt ui` keeps: it takes precedence over the PR's own state while "
            "it lasts. The lane layers (conflicts, claims by kind, agent runs, "
            "merge queue, decisions, Slack, task moves) are drawn from what "
            "`tt ui` recorded, so they begin when it first recorded each "
            "(claims and runs the earliest; conflicts only from when it first "
            "looked at the branch) and cover only tasks that were tracked "
            "branches. Before that they are unknown, not zero."
        ),
    ),
    (
        "Days, weeks and weekends",
        (
            "The server sends UTC instants and the status-mix samples; the page "
            "decides what a day, a week (Monday first) and a weekend are from "
            "your browser's time zone, so throughput buckets, lead-time dates, the "
            "weekend bands and the day labels all follow it (a Friday 7pm merge "
            "in US/Central is a Friday, not a Saturday)."
        ),
    ),
    (
        "Lead time",
        (
            "Each PR is placed at the time it merged. The 4- and 12-week views "
            "show each PR's value; longer windows show the median over the PRs "
            "merged in each week (linear interpolation between ranks; p75 and "
            "p90 likewise). A step is unknown for a PR "
            "when its start was not recorded or the request or review it is "
            "measured from does not exist: unknown is counted, never guessed. "
            "'Excluding weekends' drops the time that falls on a Saturday or "
            "Sunday in your zone (no holiday calendar); it changes lead times "
            "only, not throughput, the status mix or the cards."
        ),
    ),
    (
        "Status mix is a snapshot",
        (
            "At the start of the window, at every task state change, and now, "
            "every task in flight is in exactly one state, and the "
            "chart shows each state's share of those tasks. It is not averaged "
            "over time. An instant with no task in flight is a gap, and with only "
            "a few tasks the percentages are coarse: the number of tasks in "
            "flight is shown beside them."
        ),
    ),
    (
        "Long PRs",
        (
            "Only a PR's first 100 timeline entries are fetched; a PR with more "
            "is truncated at its newest end."
        ),
    ),
)


@dataclass(frozen=True)
class PrRecord:
    """One of the user's PRs as fetched for the window."""

    repo: str  # lower-case ``owner/name``
    head_branch: str
    facts: PrFacts
    #: The PR's review state now, as ``ReviewState`` values (``approved``,
    #: ``reviewed`` for changes requested, ``requested``, ``none``): it
    #: decides whether an approval went stale on a later push.
    review_now: str
    #: Base branch changes ``(time, previous, current)``.
    base_changes: tuple[tuple[datetime, str, str], ...] = ()
    #: GitHub's node id of the PR: the identity tasks are joined on (``""``
    #: when the payload did not carry it).
    node_id: str = ""
    #: GitHub's ``updatedAt`` of the PR (``None`` when the payload had none).
    updated_at: datetime | None = None
    url: str = ""


@dataclass(frozen=True)
class TaskFact:
    """What the stack DB knows of a branch's task."""

    repo: str | None  # lower-case ``owner/name``; None: remote unknown
    branch: str
    started_at: datetime | None
    #: When the branch stopped being tracked; ``None``: still tracked.
    removed_at: datetime | None = None
    #: Removed as the cleanup after a merge.
    landed: bool = False
    #: Still tracked, but its PR is known to be merged/closed already.
    finished: bool = False
    #: The PR recorded for the task (``None``: never recorded, so only the
    #: branch name can tie it to a PR).
    pr_node_id: str | None = None
    pr_number: int | None = None
    #: What each lane recorded for the branch (see ``lane_history``).
    lanes: tuple[api.TimelineLane, ...] = ()

    @property
    def linked(self) -> bool:
        return self.pr_node_id is not None or self.pr_number is not None


# ---- replaying a PR's events ------------------------------------------------------


@dataclass(frozen=True)
class Flags:
    draft: bool = False
    #: A review request is outstanding and the PR is not a draft.
    waiting: bool = False
    approved: bool = False
    #: A reviewer's latest review (not dismissed or superseded by an
    #: approval) requests changes.
    changes: bool = False
    failing: bool = False
    running: bool = False


_CI_DONE = "ci_done"  # a synthetic event: a commit's checks completed


def _ci_result(rollup: str | None) -> str:
    if rollup is None:
        return ""
    if rollup in act.CI_FAILED_ROLLUPS:
        return "failed"
    return "success" if rollup == "SUCCESS" else "pending"


def replay(
    facts: PrFacts,
    *,
    review_now: str,
    until: datetime,
) -> list[tuple[datetime, Flags]]:
    """The PR's flags after each event, in time order (the state holding from
    that time until the next entry). The card timeline's rules, plus
    ``changes``; ``running`` and ``failing`` follow ``act.commit_ci_phases``.
    Events after ``until`` are ignored."""
    items = [item for item in facts.items if item.time <= until]
    done = facts.state in ("MERGED", "CLOSED")
    # An approval that is no longer standing now ended at the next push.
    stale_on_push = not done and review_now not in ("approved", "")
    toggles = [
        item
        for item in items
        if item.kind in (act.READY_FOR_REVIEW, act.CONVERTED_TO_DRAFT)
    ]
    draft = toggles[0].kind == act.READY_FOR_REVIEW if toggles else facts.is_draft

    # Each commit's checks completing is an event of its own.
    synthetic = [
        TimelineItem(time=item.ci_done, kind=_CI_DONE, actor="", oid=item.oid)
        for item in items
        if item.kind == act.COMMIT
        and item.ci_done is not None
        and item.time < item.ci_done <= until
    ]
    events = sorted([*items, *synthetic], key=lambda item: item.time)

    pending: set[str] = set()
    approvers: set[str] = set()
    changes: set[str] = set()
    failing = False
    running = False
    #: Whether the head commit's checks failed once they complete.
    fails_when_done = False
    head: str | None = None
    points: list[tuple[datetime, Flags]] = [(facts.created_at, Flags(draft=draft))]
    for event in events:
        who = event.subject or event.actor
        if event.kind == act.READY_FOR_REVIEW:
            draft = False
        elif event.kind == act.CONVERTED_TO_DRAFT:
            draft = True
        elif event.kind == act.REVIEW_REQUESTED:
            pending.add(who)
        elif event.kind == act.REVIEW_REQUEST_REMOVED:
            pending.discard(who)
        elif event.kind == act.REVIEW:
            pending.discard(who)
            if event.detail == "approved":
                approvers.add(who)
                changes.discard(who)
            elif event.detail == "requested changes":
                approvers.discard(who)
                changes.add(who)
            elif event.detail == "review dismissed":
                approvers.discard(who)
                changes.discard(who)
        elif event.kind in (act.COMMIT, act.FORCE_PUSH):
            if stale_on_push:
                approvers.clear()
            phases = (
                act.commit_ci_phases(_ci_result(event.ci), event.time, event.ci_done)
                if event.kind == act.COMMIT
                else ()
            )
            first = phases[0].kind if phases else ""
            running = first == "running"
            failing = first == "failing"
            fails_when_done = len(phases) > 1 and phases[1].kind == "failing"
            head = event.oid if event.kind == act.COMMIT else None
        elif event.kind == _CI_DONE:
            if event.oid == head:
                running = False
                failing = fails_when_done
        else:
            continue
        points.append(
            (
                # A commit can predate the PR; the state holds from its
                # creation (as the card clamps to the track's start).
                max(event.time, facts.created_at),
                Flags(
                    draft=draft,
                    waiting=bool(pending) and not draft,
                    approved=bool(approvers),
                    changes=bool(changes),
                    failing=failing,
                    running=running,
                ),
            )
        )
    return points


def phase_spans(
    facts: PrFacts,
    *,
    review_now: str,
    start: datetime,
    end: datetime,
) -> list[tuple[str, datetime, datetime]]:
    """The card timeline's phases (draft, waiting, approved, ci_running,
    ci_failing) as
    ``(kind, t0, t1)``, sorted: the same result as ``phaseSpans`` in
    ``web/src/logic/timeline.ts`` for the same events (a shared test vector
    file keeps them in step)."""
    points = replay(facts, review_now=review_now, until=end)
    spans: list[tuple[str, datetime, datetime]] = []
    attributes = {"ci_running": "running", "ci_failing": "failing"}
    for kind in ("draft", "waiting", "approved", "ci_running", "ci_failing"):
        attribute = attributes.get(kind, kind)
        began: datetime | None = None
        for when, flags in points:
            when = min(max(when, start), end)
            on = getattr(flags, attribute)
            if on and began is None:
                began = when
            elif not on and began is not None:
                if when > began:
                    spans.append((kind, began, when))
                began = None
        if began is not None and end > began:
            spans.append((kind, began, end))
    return sorted(spans, key=lambda span: span[1])


# ---- a PR's life as segments ----------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    state: str
    who: str


def _state_of(flags: Flags) -> str:
    # The Next rules' priority order (see the module docstring).
    if flags.changes:
        return "changes"
    if flags.failing:
        return "ci_failing"
    if flags.running:
        return "ci_running"
    if flags.draft:
        return "draft"
    if flags.approved:
        return "approved"
    if flags.waiting:
        return "waiting"
    return "ready"


def _life(record: PrRecord, now: datetime) -> list[tuple[datetime, datetime]]:
    """The intervals the PR was open: from creation to its merge/close,
    minus the time between a close and a reopening."""
    facts = record.facts
    end = now
    if facts.state == "MERGED" and facts.merged_at is not None:
        end = facts.merged_at
    elif facts.state == "CLOSED" and facts.closed_at is not None:
        end = facts.closed_at
    intervals: list[tuple[datetime, datetime]] = []
    began: datetime | None = facts.created_at
    for item in facts.items:
        if item.time >= end:
            break
        if item.kind in (act.CLOSED, act.MERGED) and began is not None:
            if item.time > began:
                intervals.append((began, item.time))
            began = None
        elif item.kind == act.REOPENED and began is None:
            began = item.time
    if began is not None and end > began:
        intervals.append((began, end))
    return [(a, b) for a, b in intervals if b > a]


def open_segments(record: PrRecord, now: datetime) -> list[Segment]:
    life = _life(record, now)
    if not life:
        return []
    points = replay(
        record.facts,
        review_now=record.review_now,
        until=life[-1][1],
    )
    times = [when for when, _ in points]
    segments: list[Segment] = []
    for a, b in life:
        cuts = sorted({a, b, *(t for t in times if a < t < b)})
        for lo, hi in pairwise(cuts):
            index = bisect.bisect_right(times, lo) - 1
            state = _state_of(points[index][1])
            previous = segments[-1] if segments else None
            if previous and previous.state == state and previous.end == lo:
                segments[-1] = Segment(previous.start, hi, state, previous.who)
            else:
                segments.append(Segment(lo, hi, state, _who(state)))
    return segments


def _who(state: str) -> str:
    who = _STATE_BY_KEY[state].who
    return who if who is not None else WHO_OTHERS


def _base_at(record: PrRecord, when: datetime) -> str:
    changes = record.base_changes
    if not changes:
        return record.facts.base_ref
    if when < changes[0][0]:
        return changes[0][1]
    current = changes[0][2]
    for at, _, new in changes:
        if at <= when:
            current = new
    return current


def _unmerged_parent(
    records: Sequence[PrRecord], candidates: Sequence[int], base: str, when: datetime
) -> int | None:
    """The candidate PR whose head is ``base`` and that exists and is not
    merged at ``when``."""
    for index in candidates:
        facts = records[index].facts
        if records[index].head_branch != base or facts.created_at > when:
            continue
        if facts.merged_at is None or facts.merged_at > when:
            return index
    return None


def with_blocked(
    records: Sequence[PrRecord],
    segments: Sequence[list[Segment]],
    now: datetime,
) -> list[list[Segment]]:
    """Splits approved spans where the PR's base was another, not yet merged,
    PR of the same repo: "Blocked on parent", waiting on you only when the
    parent's own next action is yours (the Next rule, transitively)."""
    by_head: dict[tuple[str, str], list[int]] = {}
    for index, record in enumerate(records):
        by_head.setdefault((record.repo, record.head_branch), []).append(index)
    resolved: dict[int, list[Segment]] = {}
    visiting: set[int] = set()

    def who_at(index: int, when: datetime) -> str:
        for segment in resolve(index):
            if segment.start <= when < segment.end:
                return segment.who
        return WHO_OTHERS

    def parents_of(index: int) -> list[int]:
        record = records[index]
        bases = {record.facts.base_ref}
        for _, previous, current in record.base_changes:
            bases.update((previous, current))
        found: list[int] = []
        for base in bases:
            found += [i for i in by_head.get((record.repo, base), []) if i != index]
        return found

    def resolve(index: int) -> list[Segment]:
        if index in resolved:
            return resolved[index]
        if index in visiting:  # a cycle of bases: cannot be told apart
            return list(segments[index])
        visiting.add(index)
        record = records[index]
        candidates = parents_of(index)
        out: list[Segment] = []
        for segment in segments[index]:
            if segment.state != "approved" or not candidates:
                out.append(segment)
                continue
            cuts = {segment.start, segment.end}
            cuts.update(
                at
                for at, _, _ in record.base_changes
                if segment.start < at < segment.end
            )
            for parent in candidates:
                other = records[parent].facts
                cuts.update(
                    t
                    for t in (other.created_at, other.merged_at, other.closed_at)
                    if t is not None and segment.start < t < segment.end
                )
                cuts.update(
                    t
                    for part in segments[parent]
                    for t in (part.start, part.end)
                    if segment.start < t < segment.end
                )
            ordered = sorted(cuts)
            for lo, hi in pairwise(ordered):
                base = _base_at(record, lo)
                blocking = _unmerged_parent(records, candidates, base, lo)
                if blocking is None:
                    out.append(Segment(lo, hi, "approved", WHO_YOU))
                else:
                    who = WHO_YOU if who_at(blocking, lo) == WHO_YOU else WHO_OTHERS
                    out.append(Segment(lo, hi, "blocked", who))
        visiting.discard(index)
        resolved[index] = out
        return out

    return [resolve(index) for index in range(len(records))]


# ---- matching tasks and PRs ------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    pr: PrRecord | None
    #: A recorded task start that precedes the PR (or the whole task when
    #: there is no PR).
    started: datetime | None
    #: Without a PR: when it stopped being worked on (``None``: still going).
    ended: datetime | None = None
    #: How the task fact was tied to the PR: ``id`` (the recorded PR link)
    #: or ``name`` (the branch name and the times, the fallback for a task
    #: whose PR was never recorded); ``None`` without a fact.
    matched_by: str | None = None
    #: The lanes of the task's branch (see ``TaskFact.lanes``).
    lanes: tuple[api.TimelineLane, ...] = ()


def match_tasks(
    prs: Sequence[PrRecord], facts: Sequence[TaskFact], now: datetime
) -> list[Task]:
    """Pairs each PR with its task fact and turns the facts left over into
    tasks without a PR.

    A fact whose PR was recorded is joined by that identity (GitHub's node
    id, else repo and number): a branch name is not identity, so a reused
    name cannot mislead it. Only facts that never recorded a PR fall back to
    the head branch name (each used once: the earliest PR created after it,
    unless the branch was removed before the PR existed), and those matches
    are flagged ``matched_by == "name"``."""
    by_node = {f.pr_node_id: f for f in facts if f.pr_node_id}
    by_number = {
        (f.repo, f.pr_number): f
        for f in facts
        if f.pr_number is not None and f.repo is not None
    }
    by_key: dict[tuple[str, str], list[TaskFact]] = {}
    for fact in facts:
        if fact.repo is not None and not fact.linked:
            by_key.setdefault((fact.repo, fact.branch), []).append(fact)
    for group in by_key.values():
        group.sort(key=lambda f: (f.started_at is None, f.started_at))

    def start_of(fact: TaskFact, pr: PrRecord) -> datetime | None:
        if fact.started_at is not None and fact.started_at <= pr.facts.created_at:
            return fact.started_at
        return None

    used: set[int] = set()
    matched: dict[int, Task] = {}
    ordered = sorted(prs, key=lambda p: p.facts.created_at)
    for pr in ordered:
        linked = by_node.get(pr.node_id) if pr.node_id else None
        if linked is None:
            linked = by_number.get((pr.repo, pr.facts.number))
        if linked is not None and id(linked) not in used:
            used.add(id(linked))
            matched[id(pr)] = Task(
                pr=pr,
                started=start_of(linked, pr),
                matched_by="id",
                lanes=linked.lanes,
            )
    for pr in ordered:
        if id(pr) in matched:
            continue
        task = Task(pr=pr, started=None)
        for fact in by_key.get((pr.repo, pr.head_branch), []):
            if id(fact) in used:
                continue
            # A branch removed before this PR existed was an earlier attempt.
            if fact.removed_at is not None and fact.removed_at < pr.facts.created_at:
                continue
            used.add(id(fact))
            started = start_of(fact, pr)
            task = Task(pr=pr, started=started, matched_by="name", lanes=fact.lanes)
            break
        matched[id(pr)] = task
    tasks = [matched[id(pr)] for pr in ordered]
    for fact in facts:
        if id(fact) in used or fact.started_at is None:
            continue
        # A landed branch whose PR is not among the fetched ones merged before
        # the window; a tracked one known to be finished is likewise past; and
        # one with a recorded PR has a PR (not fetched: older than the window
        # or beyond the fetch's limit), so it is not a task without one.
        if fact.landed or fact.finished or fact.linked:
            continue
        tasks.append(
            Task(
                pr=None,
                started=fact.started_at,
                ended=fact.removed_at,
                lanes=fact.lanes,
            )
        )
    return tasks


# ---- statistics ------------------------------------------------------------------------


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _monday(when: datetime) -> datetime:
    day = when.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def window_start(now: datetime, weeks: int) -> datetime:
    """The first instant of the window: the Monday (UTC) ``weeks - 1`` weeks
    before the current week's."""
    return _monday(now) - timedelta(weeks=weeks - 1)


def _first(items: Iterable[TimelineItem], kind: str) -> datetime | None:
    times = [item.time for item in items if item.kind == kind]
    return min(times) if times else None


def milestones(task: Task, matched_by: str | None) -> api.PrMilestones:
    """The instants of a PR's life the client derives its lead times from
    (UTC; what is a day, a week or a weekend is the viewer's, so the server
    does no such arithmetic). ``None`` where the start, or the request or
    review a step is measured from, does not exist."""
    pr = task.pr
    assert pr is not None
    facts = pr.facts
    requested = _first(facts.items, act.REVIEW_REQUESTED)
    # The author's own review comments are not the review being waited for.
    reviews = [
        item.time
        for item in facts.items
        if item.kind == act.REVIEW and (item.subject or item.actor) != facts.author
    ]
    after_request = (
        [t for t in reviews if t >= requested] if requested is not None else []
    )
    ended = (
        facts.merged_at
        if facts.state == "MERGED"
        else facts.closed_at
        if facts.state == "CLOSED"
        else None
    )

    def iso(when: datetime | None) -> str | None:
        return _iso(when) if when is not None else None

    return api.PrMilestones(
        repo=pr.repo,
        number=facts.number,
        title=facts.title,
        url=pr.url,
        state=facts.state,  # type: ignore[arg-type]
        opened=_iso(facts.created_at),
        ended=iso(ended),
        task_started=iso(task.started),
        matched_by=matched_by if task.started is not None else None,  # type: ignore[arg-type]
        first_review_requested=iso(requested),
        first_review=iso(min(reviews, default=None)),
        first_review_after_request=iso(min(after_request, default=None)),
        additions=facts.additions,
        deletions=facts.deletions,
    )


# ---- the aggregate -----------------------------------------------------------------------


def _describe(state: StateDef) -> api.MixStateInfo:
    kind, key = state.source
    if kind == "phase":
        span = pr_states.PHASES[key]
        label, tone, legend = span.label, span.tone, span.legend
    else:
        dimension, value = key.split(":")
        value_state = pr_states.lookup(dimension, value)
        label, tone, legend = value_state.label, value_state.tone, value_state.legend
    return api.MixStateInfo(
        key=state.key,  # type: ignore[arg-type]
        label=label,
        tone=tone,
        legend=legend,
    )


def mix_state_infos() -> list[api.MixStateInfo]:
    return [_describe(state) for state in STATES]


def _bucket_edges(start: datetime, now: datetime, step: timedelta) -> list[datetime]:
    edges = [start]
    while edges[-1] + step < now:
        edges.append(edges[-1] + step)
    edges.append(now)
    return edges


def compute(
    prs: Sequence[PrRecord],
    facts: Sequence[TaskFact],
    *,
    now: datetime,
    weeks: int,
    archive_since: datetime,
    fetched_at: datetime,
    refreshed: int = 0,
    truncated: bool = False,
    hidden: int = 0,
    repo: str | None = None,
) -> api.Aggregate:
    """The aggregate view of the last ``weeks`` weeks (calendar weeks from
    Monday, UTC, the last one partial)."""
    start = window_start(now, weeks)
    # The client's window starts at its own Monday, up to a day before this
    # one (UTC): hand it the PRs of a little more than the window.
    margin_start = start - timedelta(days=2)
    tasks = match_tasks(prs, facts, now)
    with_pr = [task for task in tasks if task.pr is not None]
    records = [task.pr for task in with_pr if task.pr is not None]
    plain = [open_segments(record, now) for record in records]
    resolved = with_blocked(records, plain, now)

    # Every task's life as segments: working, then the PR's states, with the
    # time a claim was held as "With an agent".
    lives: list[list[Segment]] = []
    task_lanes: list[tuple[api.TimelineLane, ...]] = []
    for task, segments in zip(with_pr, resolved, strict=True):
        assert task.pr is not None
        life = list(segments)
        if task.started is not None and task.started < task.pr.facts.created_at:
            life.insert(
                0,
                Segment(task.started, task.pr.facts.created_at, "working", WHO_YOU),
            )
        lives.append(with_claims(life, claimed_intervals(task.lanes, now)))
        task_lanes.append(task.lanes)
    tasks_without_pr = 0
    for task in tasks:
        if task.pr is None and task.started is not None:
            end = task.ended or now
            if end > task.started:
                lives.append(
                    with_claims(
                        [Segment(task.started, end, "working", WHO_YOU)],
                        claimed_intervals(task.lanes, now),
                    )
                )
                task_lanes.append(task.lanes)
                tasks_without_pr += 1

    instants = sample_instants(start, now, lives, lane_boundaries(task_lanes, now))
    return api.Aggregate(
        weeks=weeks,
        window_start=_iso(start),
        window_end=_iso(now),
        fetched_at=_iso(fetched_at),
        refreshed=refreshed,
        repo=repo,
        prs=[
            milestones(task, task.matched_by)
            for task in with_pr
            if task.pr is not None and _in_window(task.pr, margin_start, now)
        ],
        mix=_mix(lives, instants),
        lanes=lane_layers(lives, task_lanes, instants, start, now),
        coverage=api.AggregateCoverage(
            task_starts_since=_iso(archive_since),
            tasks_without_pr=tasks_without_pr,
            truncated=truncated,
            hidden=hidden,
        ),
        notes=[api.AggregateNote(label=a, why=b) for a, b in NOTES],
    )


def _in_window(record: PrRecord, start: datetime, now: datetime) -> bool:
    facts = record.facts
    return start <= facts.created_at <= now or any(
        t is not None and start <= t <= now for t in (facts.merged_at, facts.closed_at)
    )


def sample_instants(
    start: datetime,
    now: datetime,
    lives: Sequence[Sequence[Segment]],
    extra: Iterable[datetime] = (),
) -> list[datetime]:
    """Every boundary where a task joins, leaves, or changes state, and
    ``extra`` ones (where a lane's span starts or ends)."""
    return sorted(
        {start, now}
        | {instant for instant in extra if start < instant < now}
        | {
            instant
            for life in lives
            for segment in life
            for instant in (segment.start, segment.end)
            if start < instant < now
        }
    )


def claimed_intervals(
    lanes: Sequence[api.TimelineLane], now: datetime
) -> list[tuple[datetime, datetime]]:
    """When a claim was held on the task, any holder and kind, as merged
    intervals (a claim still held runs to ``now``)."""
    spans = sorted(
        (
            datetime.fromisoformat(phase.start),
            datetime.fromisoformat(phase.end) if phase.end else now,
        )
        for lane in lanes
        if lane.lane == "claim"
        for phase in lane.phases
    )
    merged: list[tuple[datetime, datetime]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def with_claims(
    life: Sequence[Segment], claimed: Sequence[tuple[datetime, datetime]]
) -> list[Segment]:
    """``life`` with the parts under a claim in the "agent" state: a claim
    takes the task, whatever its PR was waiting for (as the Next rules do)."""
    result: list[Segment] = []
    for segment in life:
        cursor = segment.start
        for lo, hi in claimed:
            lo, hi = max(lo, segment.start), min(hi, segment.end)
            if hi <= lo:
                continue
            if lo > cursor:
                result.append(Segment(cursor, lo, segment.state, segment.who))
            result.append(Segment(lo, hi, "agent", WHO_AGENT))
            cursor = hi
        if cursor < segment.end:
            result.append(Segment(cursor, segment.end, segment.state, segment.who))
    return result


def lane_boundaries(
    task_lanes: Sequence[Sequence[api.TimelineLane]], now: datetime
) -> list[datetime]:
    """Where the aggregated lanes' spans start and end."""
    counted = {source.id for source in LANE_SOURCES if source.aggregate_skip is None}
    return [
        datetime.fromisoformat(edge)
        for lanes in task_lanes
        for lane in lanes
        if lane.lane in counted
        for phase in lane.phases
        for edge in (phase.start, phase.end)
        if edge is not None
    ]


def _in_flight(
    life: Sequence[Segment], instants: Sequence[datetime]
) -> list[Segment | None]:
    """The segment a task is in at each instant (``None``: not in flight).
    In flight on [start, end): a task that ended at the instant is no longer.
    (Still-open ones end at ``now``, the last instant, where they are in
    flight.)"""
    ordered = sorted(life, key=lambda segment: segment.start)
    starts = [segment.start for segment in ordered]
    found: list[Segment | None] = []
    for instant in instants:
        position = bisect.bisect_right(starts, instant) - 1
        if position < 0:
            found.append(None)
            continue
        segment = ordered[position]
        gone = segment.end < instant or (
            segment.end == instant and instant < instants[-1]
        )
        found.append(None if gone else segment)
    return found


def lane_layers(
    lives: Sequence[Sequence[Segment]],
    task_lanes: Sequence[Sequence[api.TimelineLane]],
    instants: Sequence[datetime],
    start: datetime,
    now: datetime,
) -> list[api.AggregateLane]:
    """Each counted lane's layer: at every instant, how many of the tasks in
    flight had a span of each kind of the lane, and when its markers happened."""
    count = len(instants)
    inflight = [0] * count
    flights = [_in_flight(life, instants) for life in lives]
    for flight in flights:
        for index, segment in enumerate(flight):
            inflight[index] += segment is not None
    layers: list[api.AggregateLane] = []
    for source in LANE_SOURCES:
        if source.aggregate_skip is not None:
            continue
        counts = {kind: [0] * count for kind in source.phases}
        times: dict[str, list[datetime]] = {kind: [] for kind in source.events}
        for flight, lanes in zip(flights, task_lanes, strict=True):
            for lane in lanes:
                if lane.lane != source.id:
                    continue
                for kind in source.phases:
                    spans = [
                        (
                            datetime.fromisoformat(phase.start),
                            datetime.fromisoformat(phase.end) if phase.end else None,
                        )
                        for phase in lane.phases
                        if phase.kind == kind
                    ]
                    for index, instant in enumerate(instants):
                        if flight[index] is not None and any(
                            lo <= instant and (hi is None or instant < hi)
                            for lo, hi in spans
                        ):
                            counts[kind][index] += 1
                for event in lane.events:
                    at = datetime.fromisoformat(event.t)
                    if start <= at <= now:
                        times[event.kind].append(at)
        layers.append(
            api.AggregateLane(
                lane=source.id,  # type: ignore[arg-type]
                phases=[
                    api.AggregateLanePhase(
                        kind=kind,  # type: ignore[arg-type]
                        share=[
                            c / n if n else None
                            for c, n in zip(counts[kind], inflight, strict=True)
                        ],
                        count=counts[kind],
                    )
                    for kind in source.phases
                ],
                events=[
                    api.AggregateLaneEvents(
                        kind=kind,  # type: ignore[arg-type]
                        times=[_iso(at) for at in sorted(times[kind])],
                    )
                    for kind in source.events
                ],
            )
        )
    return layers


def _mix(
    lives: Sequence[Sequence[Segment]],
    instants: Sequence[datetime],
) -> api.StatusMix:
    """A snapshot of the tasks in flight at each instant: every task has
    exactly one state then, and the series are each state's share of them
    (and its count). An instant with no task in flight has no shares."""
    count = len(instants)
    by_state = {state.key: [0] * count for state in STATES}
    by_who = {who: [0] * count for who in WHOS}
    for life in lives:
        for index, segment in enumerate(_in_flight(life, instants)):
            if segment is not None:
                by_state[segment.state][index] += 1
                by_who[segment.who][index] += 1
    inflight = [sum(by_state[state.key][i] for state in STATES) for i in range(count)]

    def share(counts: list[int]) -> list[float | None]:
        return [c / n if n else None for c, n in zip(counts, inflight, strict=True)]

    return api.StatusMix(
        step_hours=0,
        starts=[_iso(instant) for instant in instants],
        inflight=inflight,
        states=mix_state_infos(),
        series=[
            api.MixSeries(
                key=state.key,  # type: ignore[arg-type]
                share=share(by_state[state.key]),
                count=by_state[state.key],
            )
            for state in STATES
        ],
        who=[
            api.MixWhoSeries(
                key=who,  # type: ignore[arg-type]
                share=share(by_who[who]),
                count=by_who[who],
            )
            for who in WHOS
        ],
    )
