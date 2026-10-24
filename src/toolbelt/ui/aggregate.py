"""How work has flowed: the aggregate view's numbers.

Pure functions over what was fetched (``PrRecord``: the user's PRs of a
window with their timelines; ``TaskFact``: when each branch's task started,
from the stack DB's tracked and archived rows) and a clock. Nothing here does
I/O; ``aggregate_feed.py`` fetches and caches the inputs.

**What a task is.** A PR, plus the working time before it when the task's
start is known; or, for a branch that never got a PR, only the stack DB row.
Time in flight runs from the task's start (else the PR's creation) until the
PR merged or closed (a reopened PR counts again from the reopening).

**Status over time is Next.** A task's status at each moment is its Next
(``next_action.py``), the same one the card shows: ``task_segments`` runs
``next_action`` itself at every moment something it reads changed, so there is
no second set of states to keep in step. The PR's own state (draft, review,
CI) is replayed from its timeline; conflicts, open review threads, claims,
decisions and Slack posts come from the task's lanes (``lane_history``), which
hold what ``tt ui`` recorded. The card timeline draws the same function's
result as its "next" lane, and ``Aggregate.lanes`` aggregates every lane the
registry counts (``timeline_lanes.LANE_SOURCES`` without an ``aggregate_skip``):
at each sampled instant, how many of the tasks in flight had a span of each of
its kinds, and when its markers happened. "Who is it waiting on" groups the
"next" lane by ``who``. A lane added to the registry gets a layer, or says why
not. The draft, waiting, approved and CI-failing spans still follow the card
timeline's phase rules exactly (``phase_spans`` mirrors ``phaseSpans`` in
``web/src/logic/timeline.ts``; one shared set of vectors is checked against
both).

**What history cannot give.** Where nothing was recorded (before ``tt ui``
first saw a branch, or for a PR that was never a tracked branch), conflicts,
unresolved review threads, claims, decisions and Slack posts are absent, so
Next there is what the PR's own timeline alone gives, and the Slack rules
apply only from when posts began to be recorded. CI follows
``pr_activity.commit_ci_phases``, as the card does: "running" is the time from
a push until its check suites completed (their ``updatedAt``) whatever the
outcome, or until now for a still-pending head commit; "failing" is the time
from that completion with a failing result until the next push. A PR whose
timeline is longer than the fetched 100 items is truncated at its newest end.
"""

import bisect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from toolbelt.git.stack.status import (
    NO_PR,
    BranchStatus,
    CiState,
    PrState,
    ReviewState,
    is_bot,
)
from toolbelt.git.stack.store import AgentTurn
from toolbelt.ui import api_models as api
from toolbelt.ui import next_action as na
from toolbelt.ui import pr_activity as act
from toolbelt.ui.pr_activity import PrFacts, TimelineItem
from toolbelt.ui.slack_posts import DEFAULT_BUMP_AFTER
from toolbelt.ui.timeline_lanes import LANE_SOURCES, next_phase

WINDOWS = (4, 12, 26, 52)
DEFAULT_WEEKS = 12

WHO_YOU = na.WHO_YOU
WHO_AGENT = na.WHO_AGENT
WHO_AUTO = na.WHO_AUTO
WHO_CI = na.WHO_CI
WHO_OTHERS = na.WHO_OTHERS
#: Who a task can be waiting on, in the order "needs me first" sorts them
#: (``next_action.WHO_ORDER`` without "none": a finished task is not in flight).
WHOS = tuple(who for who in na.WHO_ORDER if who != na.WHO_NONE)

#: The Slack rules of Next (post it, bump it) apply from this long after the
#: last post (``slack_posts.DEFAULT_BUMP_AFTER``).
SLACK_BUMP_AFTER = DEFAULT_BUMP_AFTER


NOTES: tuple[tuple[str, str], ...] = (
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
        "Recorded lanes",
        (
            "Claims, conflicts, open review threads, decisions, Slack posts, "
            "agent runs, the merge queue and task moves are drawn from what "
            "`tt ui` recorded, so each begins when it was first recorded and "
            "covers only tasks that were tracked branches; before that it is "
            'unknown, not zero. A claim takes the task ("With an agent" or '
            '"Autonomous agent") only while its holder was working, and a '
            'question for you ("Deciding") takes it over any claim, as on the '
            "cards."
        ),
    ),
    (
        "Agent activity",
        (
            "Recorded by the Claude Code plugin from when it was installed and "
            "updated (earlier turns are unknown), for every session on this "
            "machine. A turn's wall time is split into tool time (calls, less "
            "time blocked on you and waiting on subagents), delegated time "
            "(waiting on subagents), blocked time (permission prompts and "
            "dialogs) and model time (the rest: thinking, streaming, API "
            "latency); the parts add up to it, and parallel calls count once. "
            "Call time by tool and by category is summed over calls, so "
            "parallel calls add up past the wall time. A turn is counted in the "
            "hour it started. Turns are tied to a repo by the worktree most of "
            "their calls were about; the rest are only in the all-repos view. "
            "Raw calls are kept for 60 days, the per-turn numbers for good."
        ),
    ),
    (
        "Days, weeks and weekends",
        (
            "The server sends UTC instants and the status samples; the page "
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
        "Status over time is a snapshot",
        (
            "At the start of the window, at every task or lane change, and now, "
            "every task in flight is in exactly one Next state, and the "
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


def _active(
    lanes: Sequence[api.TimelineLane], lane: str, now: datetime
) -> list[tuple[datetime, datetime, api.LanePhase]]:
    """A lane's spans as ``(start, end, span)``; one still going ends at
    ``now``."""
    return [
        (
            datetime.fromisoformat(phase.start),
            datetime.fromisoformat(phase.end) if phase.end else now,
            phase,
        )
        for found in lanes
        if found.lane == lane
        for phase in found.phases
    ]


def _on(
    spans: Sequence[tuple[datetime, datetime, api.LanePhase]], at: datetime
) -> bool:
    return any(lo <= at < hi for lo, hi, _ in spans)


def _review_of(flags: Flags) -> ReviewState:
    if flags.changes:
        return ReviewState.REVIEWED
    if flags.approved:
        return ReviewState.APPROVED
    if flags.waiting:
        return ReviewState.REQUESTED
    return ReviewState.NONE


def task_segments(
    pr: PrRecord | None,
    started: datetime | None,
    ended: datetime | None,
    lanes: Sequence[api.TimelineLane],
    slack_from: datetime | None,
    now: datetime,
) -> list[Segment]:
    """A task's Next over its life: ``next_action`` run at every moment
    something it reads changed, on what history gives it. The PR's own state
    is replayed from its timeline (``replay``); conflicts, open review threads,
    claims (a *working* holder takes the task), pending decisions and Slack
    posts come from the task's lanes (what was recorded), and are absent where
    nothing was. The Slack rules apply from ``slack_from`` (when Slack posts
    began to be recorded). A task is in flight from its recorded start (else
    its PR's creation) until the PR ended; time with the PR closed is not.
    "Blocked on a parent" needs the other tasks: see ``with_blocked``."""
    slices: list[tuple[datetime, datetime]] = []
    points: list[tuple[datetime, Flags]] = []
    if pr is not None:
        life = _life(pr, now)
        if started is not None and started < pr.facts.created_at:
            slices.append((started, pr.facts.created_at))
        slices += life
        if life:
            points = replay(pr.facts, review_now=pr.review_now, until=life[-1][1])
    elif started is not None and (ended or now) > started:
        slices.append((started, ended or now))
    if not slices:
        return []

    conflicts = _active(lanes, "git", now)
    threads = _active(lanes, "review", now)
    claims = _active(lanes, "claim", now)
    decisions = _active(lanes, "decision", now)
    validated = _active(lanes, "personal_review", now)
    posts = sorted(
        datetime.fromisoformat(event.t)
        for lane in lanes
        if lane.lane == "slack"
        for event in lane.events
    )
    changes = {when for when, _ in points}
    for spans in (conflicts, threads, claims, decisions, validated):
        changes.update(edge for lo, hi, _ in spans for edge in (lo, hi))
    if slack_from is not None:
        changes.update(
            {slack_from, *posts, *(post + SLACK_BUMP_AFTER for post in posts)}
        )
    times = [when for when, _ in points]

    def action_at(at: datetime) -> na.NextAction:
        flags = None
        if pr is not None and points and at >= pr.facts.created_at:
            flags = points[bisect.bisect_right(times, at) - 1][1]
        has_threads = _on(threads, at)
        if flags is None:
            status = NO_PR
        else:
            status = BranchStatus(
                pr=PrState.DRAFT if flags.draft else PrState.OPEN,
                ci=CiState.FAILED
                if flags.failing
                else CiState.RUNNING
                if flags.running
                else CiState.SUCCESS,
                review=_review_of(flags),
                url=None,
                open_comments=int(has_threads),
                has_conflicts=_on(conflicts, at),
            )
        working = [
            phase.kind.split("_")[1]
            for lo, hi, phase in claims
            if lo <= at < hi and phase.state in ("", "working")
        ]
        slack = None
        if slack_from is not None and at >= slack_from:
            before = [post for post in posts if post <= at]
            slack = na.SlackContext(posted=bool(before))
            if before and at - before[-1] >= SLACK_BUMP_AFTER:
                looked = (
                    has_threads
                    or status.review in (ReviewState.APPROVED, ReviewState.REVIEWED)
                    or (
                        pr is not None
                        and any(
                            item.kind == act.REVIEW
                            and before[0] < item.time <= at
                            and item.actor != pr.facts.author
                            and not is_bot(item.actor)
                            for item in pr.facts.items
                        )
                    )
                )
                slack = na.SlackContext(posted=True, bump_due=not looked)
        return na.next_action(
            status,
            None,
            claimed=any(holder != "autonomous" for holder in working),
            autonomous="autonomous" in working,
            slack=slack,
            deciding=_on(decisions, at),
            validated=_on(validated, at),
        )

    segments: list[Segment] = []
    for lo, hi in slices:
        cuts = sorted({lo, hi, *(when for when in changes if lo < when < hi)})
        for start, end in pairwise(cuts):
            action = action_at(start)
            previous = segments[-1] if segments else None
            if (
                previous
                and previous.end == start
                and (previous.state, previous.who) == (action.kind, action.who)
            ):
                segments[-1] = Segment(previous.start, end, action.kind, action.who)
            else:
                segments.append(Segment(start, end, action.kind, action.who))
    return segments


def open_segments(record: PrRecord, now: datetime) -> list[Segment]:
    """A PR's Next while it was open, on its own timeline alone."""
    return task_segments(record, None, None, (), None, now)


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
            if segment.state != na.MERGE or not candidates:
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
                    out.append(Segment(lo, hi, na.MERGE, WHO_YOU))
                else:
                    who = WHO_YOU if who_at(blocking, lo) == WHO_YOU else WHO_OTHERS
                    out.append(Segment(lo, hi, na.BLOCKED, who))
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


def _bucket_edges(start: datetime, now: datetime, step: timedelta) -> list[datetime]:
    edges = [start]
    while edges[-1] + step < now:
        edges.append(edges[-1] + step)
    edges.append(now)
    return edges


def pr_lives(
    tasks: Sequence[Task], now: datetime
) -> tuple[list[Task], list[list[Segment]], datetime | None]:
    """The tasks that have a PR and each one's Next over its life (blocked on
    a parent resolved), and when Slack posts began to be recorded."""
    with_pr = [task for task in tasks if task.pr is not None]
    records = [task.pr for task in with_pr if task.pr is not None]
    slack_from = min(
        (
            datetime.fromisoformat(event.t)
            for task in tasks
            for lane in task.lanes
            if lane.lane == "slack"
            for event in lane.events
        ),
        default=None,
    )
    plain = [
        task_segments(task.pr, task.started, None, task.lanes, slack_from, now)
        for task in with_pr
    ]
    return with_pr, with_blocked(records, plain, now), slack_from


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
    turns: Sequence[AgentTurn] = (),
) -> api.Aggregate:
    """The aggregate view of the last ``weeks`` weeks (calendar weeks from
    Monday, UTC, the last one partial)."""
    start = window_start(now, weeks)
    # The client's window starts at its own Monday, up to a day before this
    # one (UTC): hand it the PRs of a little more than the window.
    margin_start = start - timedelta(days=2)
    tasks = match_tasks(prs, facts, now)
    with_pr, resolved, slack_from = pr_lives(tasks, now)

    # Every task's Next over its life, and the lanes the layers are drawn from
    # (its own plus the "next" lane that life is).
    lives: list[list[Segment]] = []
    task_lanes: list[tuple[api.TimelineLane, ...]] = []
    for task, segments in zip(with_pr, resolved, strict=True):
        lives.append(list(segments))
        task_lanes.append(with_next_lane(task.lanes, segments, now))
    tasks_without_pr = 0
    for task in tasks:
        if task.pr is None and task.started is not None:
            segments = task_segments(
                None, task.started, task.ended, task.lanes, slack_from, now
            )
            if segments:
                lives.append(segments)
                task_lanes.append(with_next_lane(task.lanes, segments, now))
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
        samples=api.AggregateSamples(
            starts=[_iso(instant) for instant in instants],
            inflight=_inflight_counts(lives, instants),
        ),
        waiting_on=waiting_on(lives, instants),
        activity=activity_hours(turns, margin_start),
        lanes=lane_layers(lives, task_lanes, instants, start, now),
        coverage=api.AggregateCoverage(
            task_starts_since=_iso(archive_since),
            tasks_without_pr=tasks_without_pr,
            truncated=truncated,
            hidden=hidden,
        ),
        notes=[api.AggregateNote(label=a, why=b) for a, b in NOTES],
    )


def activity_hours(
    turns: Iterable[AgentTurn], start: datetime
) -> api.AggregateActivity:
    """Agent turns that started since ``start``, summed per hour (UTC); the
    server does no calendar arithmetic, the page makes days of them."""
    hours: dict[datetime, list[AgentTurn]] = {}
    first: datetime | None = None
    for turn in turns:
        first = turn.started_at if first is None else min(first, turn.started_at)
        if turn.started_at >= start:
            hour = turn.started_at.astimezone(UTC).replace(
                minute=0, second=0, microsecond=0
            )
            hours.setdefault(hour, []).append(turn)

    def buckets(
        group: list[AgentTurn], pick: Callable[[AgentTurn], dict[str, tuple[int, int]]]
    ) -> list[api.ActivityBucket]:
        total: dict[str, list[int]] = {}
        for turn in group:
            for name, (calls, ms) in pick(turn).items():
                entry = total.setdefault(name, [0, 0])
                entry[0] += calls
                entry[1] += ms
        return [
            api.ActivityBucket(name=name, calls=calls, ms=ms)
            for name, (calls, ms) in sorted(total.items(), key=lambda kv: -kv[1][1])
        ]

    return api.AggregateActivity(
        hours=[
            api.ActivityHour(
                t=_iso(hour),
                turns=len(group),
                calls=sum(t.calls for t in group),
                wall_ms=sum(t.totals.wall_ms for t in group),
                tool_ms=sum(t.totals.tool_ms for t in group),
                delegated_ms=sum(t.totals.delegated_ms for t in group),
                blocked_ms=sum(t.totals.blocked_ms for t in group),
                model_ms=sum(t.totals.model_ms for t in group),
                tools=buckets(group, lambda t: t.by_tool),
                categories=buckets(group, lambda t: t.by_category),
            )
            for hour, group in sorted(hours.items())
        ],
        since=_iso(first) if first is not None else None,
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


def with_next_lane(
    lanes: Sequence[api.TimelineLane], segments: Sequence[Segment], now: datetime
) -> tuple[api.TimelineLane, ...]:
    """The task's lanes with its Next (``segments``) as the "next" lane; the
    span that runs to ``now`` is still going."""
    return (
        api.TimelineLane(
            lane="next",
            events=[],
            phases=[
                api.LanePhase.model_validate(
                    {
                        "start": _iso(segment.start),
                        "end": _iso(segment.end) if segment.end < now else None,
                        "kind": next_phase(segment.state),
                        "detail": "",
                        "state": "",
                    }
                )
                for segment in segments
            ],
        ),
        *lanes,
    )


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


def _inflight_counts(
    lives: Sequence[Sequence[Segment]], instants: Sequence[datetime]
) -> list[int]:
    """How many tasks are in flight at each instant."""
    counts = [0] * len(instants)
    for life in lives:
        for index, segment in enumerate(_in_flight(life, instants)):
            counts[index] += segment is not None
    return counts


def waiting_on(
    lives: Sequence[Sequence[Segment]], instants: Sequence[datetime]
) -> list[api.WaitingOn]:
    """Who the tasks in flight are waiting on at each instant: their Next,
    grouped by who it waits on (the "next" lane, by ``who``)."""
    count = len(instants)
    by_who = {who: [0] * count for who in WHOS}
    for life in lives:
        for index, segment in enumerate(_in_flight(life, instants)):
            if segment is not None:
                by_who[segment.who][index] += 1
    inflight = _inflight_counts(lives, instants)
    return [
        api.WaitingOn(
            key=who,  # type: ignore[arg-type]
            share=[
                c / n if n else None for c, n in zip(by_who[who], inflight, strict=True)
            ],
            count=by_who[who],
        )
        for who in WHOS
    ]
