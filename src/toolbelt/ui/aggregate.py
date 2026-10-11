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

**What history cannot give.** Merge conflicts and unresolved review threads
are only known now (``pr_states.NOT_REPRESENTED``), so "Resolve conflicts" /
"Resolve comments" never appear; time spent there is shown as whatever else
applies. CI "running" is the time between a push and when its check suites
completed (their ``updatedAt``), or until now for a still-pending head
commit; a commit that fails counts as failing from the push, as the card
draws it. A PR whose timeline is longer than the fetched 100 items is
truncated at its newest end.
"""

import bisect
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from toolbelt.ui import api_models as api
from toolbelt.ui import pr_activity as act
from toolbelt.ui import pr_states
from toolbelt.ui.pr_activity import PrFacts, TimelineItem

WINDOWS = (4, 12, 26, 52)
DEFAULT_WEEKS = 12
#: Windows up to this many weeks are bucketed per day, longer ones per week.
DAILY_UP_TO_WEEKS = 12

WHO_YOU = "you"
WHO_CI = "ci"
WHO_OTHERS = "others"
WHOS = (WHO_YOU, WHO_CI, WHO_OTHERS)


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
    StateDef("ci_running", ("state", "next:wait_ci"), WHO_CI),
    StateDef("ci_failing", ("phase", "ci_failing"), WHO_YOU),
    StateDef("approved", ("phase", "approved"), WHO_YOU),
    StateDef("blocked", ("state", "next:blocked"), None),
)
_STATE_BY_KEY = {state.key: state for state in STATES}

NOTES: tuple[tuple[str, str], ...] = (
    (
        "Merge conflicts and unresolved threads",
        "GitHub keeps no history of either (the card shows them as now-only "
        "states), so 'Resolve conflicts' and 'Resolve comments' are not "
        "separated here: that time is counted as whatever else applies.",
    ),
    (
        "Waiting on CI",
        "From a push until its check suites completed. A commit that fails "
        "counts as 'CI failing' from the push (as on the card), so only "
        "passing or still-pending runs show as waiting.",
    ),
    (
        "Tasks before their PR",
        "Only tasks whose start was recorded (a branch created with toolbelt) "
        "have a working span; others begin when their PR opened.",
    ),
    (
        "Blocked on a parent",
        "Rebuilt from each PR's base branch over time and the parent PR's "
        "state, with the same rule as Next: it waits on you when the parent "
        "does.",
    ),
    (
        "Weighting",
        "Each bucket is the share of the task time spent in a state "
        "(time-weighted), so a task counts for as long as it was in flight.",
    ),
    (
        "Long PRs",
        "Only a PR's first 100 timeline entries are fetched; a PR with more "
        "is truncated at its newest end.",
    ),
)

#: The lead-time intervals, in the order of a PR's life.
LEAD_STAGES: tuple[tuple[str, str], ...] = (
    ("start_to_open", "Task started to PR opened"),
    ("open_to_request", "PR opened to review requested"),
    ("request_to_review", "Review requested to first review"),
    ("review_to_merge", "First review to merged"),
    ("start_to_merge", "Task started to merged"),
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
    #: When the check suites of a commit (by oid) completed.
    ci_done: Mapping[str, datetime] = field(default_factory=dict)


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
    ci_done: Mapping[str, datetime] | None = None,
    until: datetime,
) -> list[tuple[datetime, Flags]]:
    """The PR's flags after each event, in time order (the state holding from
    that time until the next entry). The card timeline's rules, plus
    ``changes`` and ``running``. Events after ``until`` are ignored."""
    ci_done = ci_done or {}
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
        TimelineItem(time=when, kind=_CI_DONE, actor="", oid=item.oid)
        for item in items
        if item.kind == act.COMMIT
        and (when := ci_done.get(item.oid)) is not None
        and item.time < when <= until
    ]
    events = sorted([*items, *synthetic], key=lambda item: item.time)

    pending: set[str] = set()
    approvers: set[str] = set()
    changes: set[str] = set()
    failing = False
    running = False
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
            result = _ci_result(event.ci) if event.kind == act.COMMIT else ""
            failing = result == "failed"
            # Still running: pending now, or its suites finish later.
            running = event.kind == act.COMMIT and (
                result == "pending"
                or (
                    result == "success"
                    and ci_done.get(event.oid, event.time) > event.time
                )
            )
            head = event.oid if event.kind == act.COMMIT else None
        elif event.kind == _CI_DONE:
            if event.oid == head:
                running = False
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
    """The card timeline's phases (draft, waiting, approved, ci_failing) as
    ``(kind, t0, t1)``, sorted: the same result as ``phaseSpans`` in
    ``web/src/logic/timeline.ts`` for the same events (a shared test vector
    file keeps them in step)."""
    points = replay(facts, review_now=review_now, until=end)
    spans: list[tuple[str, datetime, datetime]] = []
    for kind in ("draft", "waiting", "approved", "ci_failing"):
        attribute = "failing" if kind == "ci_failing" else kind
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
        ci_done=record.ci_done,
        until=life[-1][1],
    )
    times = [when for when, _ in points]
    segments: list[Segment] = []
    for a, b in life:
        cuts = sorted({a, b, *(t for t in times if a < t < b)})
        for lo, hi in zip(cuts, cuts[1:], strict=False):
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
            for lo, hi in zip(ordered, ordered[1:], strict=False):
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


def match_tasks(
    prs: Sequence[PrRecord], facts: Sequence[TaskFact], now: datetime
) -> list[Task]:
    """Pairs each PR with the task fact of its head branch (each fact is used
    once: the earliest PR created after it), and turns the facts left over
    into tasks without a PR."""
    by_key: dict[tuple[str, str], list[TaskFact]] = {}
    for fact in facts:
        if fact.repo is not None:
            by_key.setdefault((fact.repo, fact.branch), []).append(fact)
    for group in by_key.values():
        group.sort(key=lambda f: (f.started_at is None, f.started_at))
    used: set[int] = set()
    tasks: list[Task] = []
    for pr in sorted(prs, key=lambda p: p.facts.created_at):
        started: datetime | None = None
        for fact in by_key.get((pr.repo, pr.head_branch), []):
            if id(fact) in used:
                continue
            # A branch removed before this PR existed was an earlier attempt.
            if fact.removed_at is not None and fact.removed_at < pr.facts.created_at:
                continue
            used.add(id(fact))
            if fact.started_at is not None and fact.started_at <= pr.facts.created_at:
                started = fact.started_at
            break
        tasks.append(Task(pr=pr, started=started))
    for fact in facts:
        if id(fact) in used or fact.started_at is None:
            continue
        # A landed branch whose PR is not among the fetched ones merged before
        # the window; a tracked one known to be finished is likewise past.
        if fact.landed or fact.finished:
            continue
        tasks.append(Task(pr=None, started=fact.started_at, ended=fact.removed_at))
    return tasks


# ---- statistics ------------------------------------------------------------------------


def percentile(sorted_values: Sequence[float], fraction: float) -> float | None:
    """Linear interpolation between closest ranks; ``None`` when empty."""
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (
        position - low
    )


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _monday(when: datetime) -> datetime:
    day = when.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return day - timedelta(days=day.weekday())


def window_start(now: datetime, weeks: int) -> datetime:
    """The first instant of the window: the Monday (UTC) ``weeks - 1`` weeks
    before the current week's."""
    return _monday(now) - timedelta(weeks=weeks - 1)


def _first(items: Iterable[TimelineItem], kind: str) -> datetime | None:
    times = [item.time for item in items if item.kind == kind]
    return min(times) if times else None


def lead_intervals(task: Task) -> dict[str, float | None]:
    """The lead-time intervals of a merged PR in seconds; ``None`` where the
    start or the request/review it is measured from does not exist."""
    pr = task.pr
    assert pr is not None and pr.facts.merged_at is not None
    facts = pr.facts
    merged = facts.merged_at
    assert merged is not None
    requested = _first(facts.items, act.REVIEW_REQUESTED)
    # The author's own review comments are not the review being waited for.
    reviews = [
        item.time
        for item in facts.items
        if item.kind == act.REVIEW and (item.subject or item.actor) != facts.author
    ]
    first_review = min(reviews) if reviews else None
    after_request = (
        [t for t in reviews if requested is not None and t >= requested]
        if requested
        else []
    )

    def seconds(a: datetime | None, b: datetime | None) -> float | None:
        if a is None or b is None:
            return None
        return max((b - a).total_seconds(), 0.0)

    return {
        "start_to_open": seconds(task.started, facts.created_at),
        "open_to_request": seconds(facts.created_at, requested),
        "request_to_review": seconds(requested, min(after_request, default=None)),
        "review_to_merge": seconds(first_review, merged),
        "start_to_merge": seconds(task.started, merged),
    }


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
    truncated: bool = False,
    hidden: int = 0,
) -> api.Aggregate:
    """The aggregate view of the last ``weeks`` weeks (calendar weeks from
    Monday, UTC, the last one partial)."""
    start = window_start(now, weeks)
    tasks = match_tasks(prs, facts, now)
    with_pr = [task for task in tasks if task.pr is not None]
    records = [task.pr for task in with_pr if task.pr is not None]
    plain = [open_segments(record, now) for record in records]
    resolved = with_blocked(records, plain, now)

    # Every task's life as segments: working, then the PR's states.
    lives: list[list[Segment]] = []
    for task, segments in zip(with_pr, resolved, strict=True):
        assert task.pr is not None
        life = list(segments)
        if task.started is not None and task.started < task.pr.facts.created_at:
            life.insert(
                0,
                Segment(task.started, task.pr.facts.created_at, "working", WHO_YOU),
            )
        lives.append(life)
    tasks_without_pr = 0
    for task in tasks:
        if task.pr is None and task.started is not None:
            end = task.ended or now
            if end > task.started:
                lives.append([Segment(task.started, end, "working", WHO_YOU)])
                tasks_without_pr += 1

    return api.Aggregate(
        weeks=weeks,
        window_start=_iso(start),
        window_end=_iso(now),
        fetched_at=_iso(fetched_at),
        throughput=_throughput(records, start, now),
        lead_time=_lead_time(with_pr, start),
        mix=_mix(lives, start, now, weeks),
        coverage=api.AggregateCoverage(
            prs=sum(1 for r in records if _in_window(r, start, now)),
            prs_with_start=sum(
                1
                for task in with_pr
                if task.started is not None
                and task.pr is not None
                and _in_window(task.pr, start, now)
            ),
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


def _throughput(
    records: Sequence[PrRecord], start: datetime, now: datetime
) -> api.Throughput:
    weeks = []
    when = start
    while when <= now:
        weeks.append(when)
        when += timedelta(weeks=1)
    opened = [0] * len(weeks)
    merged = [0] * len(weeks)
    closed = [0] * len(weeks)

    def bucket(at: datetime) -> int | None:
        if at < start or at > now:
            return None
        return bisect.bisect_right(weeks, at) - 1

    for record in records:
        facts = record.facts
        index = bucket(facts.created_at)
        if index is not None:
            opened[index] += 1
        if facts.state == "MERGED" and facts.merged_at is not None:
            index = bucket(facts.merged_at)
            if index is not None:
                merged[index] += 1
        elif facts.state == "CLOSED" and facts.closed_at is not None:
            index = bucket(facts.closed_at)
            if index is not None:
                closed[index] += 1
    return api.Throughput(
        starts=[_iso(w) for w in weeks], opened=opened, merged=merged, closed=closed
    )


def _lead_time(tasks: Sequence[Task], start: datetime) -> list[api.LeadStage]:
    merged_in_window = [
        task
        for task in tasks
        if task.pr is not None
        and task.pr.facts.state == "MERGED"
        and task.pr.facts.merged_at is not None
        and task.pr.facts.merged_at >= start
    ]
    intervals = [lead_intervals(task) for task in merged_in_window]
    stages = []
    for key, label in LEAD_STAGES:
        values = sorted(v for i in intervals if (v := i[key]) is not None)
        stages.append(
            api.LeadStage(
                key=key,  # type: ignore[arg-type]
                label=label,
                n=len(values),
                unknown=len(intervals) - len(values),
                median_s=percentile(values, 0.5),
                p75_s=percentile(values, 0.75),
                p90_s=percentile(values, 0.9),
                values_s=values,
            )
        )
    return stages


def _mix(
    lives: Sequence[Sequence[Segment]],
    window_start: datetime,
    now: datetime,
    weeks: int,
) -> api.StatusMix:
    daily = weeks <= DAILY_UP_TO_WEEKS
    edges = _bucket_edges(
        window_start, now, timedelta(days=1) if daily else timedelta(weeks=1)
    )
    count = len(edges) - 1
    by_state = {state.key: [0.0] * count for state in STATES}
    by_who = {who: [0.0] * count for who in WHOS}
    for life in lives:
        for segment in life:
            if segment.end <= window_start or segment.start >= now:
                continue
            first = max(bisect.bisect_right(edges, segment.start) - 1, 0)
            for index in range(first, count):
                if edges[index] >= segment.end:
                    break
                lo = max(edges[index], segment.start)
                hi = min(edges[index + 1], segment.end)
                if hi > lo:
                    seconds = (hi - lo).total_seconds()
                    by_state[segment.state][index] += seconds
                    by_who[segment.who][index] += seconds
    totals = [sum(by_state[state.key][i] for state in STATES) for i in range(count)]

    def shares(values: list[float]) -> list[float | None]:
        return [v / t if t > 0 else None for v, t in zip(values, totals, strict=True)]

    return api.StatusMix(
        granularity="day" if daily else "week",
        starts=[_iso(edge) for edge in edges[:-1]],
        inflight=[
            total / (edges[i + 1] - edges[i]).total_seconds()
            for i, total in enumerate(totals)
        ],
        states=mix_state_infos(),
        series=[
            api.MixSeries(key=state.key, share=shares(by_state[state.key]))  # type: ignore[arg-type]
            for state in STATES
        ],
        who=[
            api.MixWhoSeries(key=who, share=shares(by_who[who]))  # type: ignore[arg-type]
            for who in WHOS
        ],
    )
