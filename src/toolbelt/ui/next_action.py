"""The single "what happens next on this branch, and who is it waiting on".

``next_action`` is a pure function of a branch's PR status and its stack
context. Rules are checked in priority order; the first that applies wins, so a
PR with conflicts *and* failing CI says "Resolve conflicts" (fix the thing
that blocks the others first).

========================  =========================  ======
condition                 label                      who
========================  =========================  ======
no PR                     Open PR                    you
merged / closed           Done                       none
merge conflicts           Resolve conflicts          you
changes requested         Address review             you
unresolved threads        Resolve comments           you
CI failed                 Fix / re-run CI            you
draft, CI running         Waiting on CI              ci
draft (CI green / none)   Publish PR                 you
open, CI running          Waiting on CI              ci
approved, parent PR not   Blocked on <parent>        you if the parent's own
merged (stacked)                                     next action is yours,
                                                     else others
approved                  Merge                      you
review requested          Waiting for review         others
otherwise                 Request review             you
========================  =========================  ======

"Blocked on <parent>" replaces "Merge" only: a child's review can proceed
while its parent is open, but it must not be merged before the parent. Who it
waits on follows the parent's own next action: when that is yours (the parent
needs you to fix CI, merge, ...) the stack is work for you, so the child reads
``you`` too; when the parent waits on others or CI, so does the child. A chain
inherits this transitively.
"""

from dataclasses import dataclass
from typing import Literal

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState

WHO_YOU = "you"
WHO_OTHERS = "others"
WHO_CI = "ci"
WHO_NONE = "none"

#: Who-values in the order "needs me first" sorts them.
WHO_ORDER = [WHO_YOU, WHO_CI, WHO_OTHERS, WHO_NONE]

#: The values above as a type, for the API's wire models (a test keeps the
#: two in step).
type Who = Literal["you", "others", "ci", "none"]


# The rule identifiers a ``NextAction.kind`` can take (see the table above).
# ``pr_states`` must give each one a timeline representation; a test checks
# that these are exactly the kinds ``next_action`` can return.
OPEN_PR = "open_pr"
DONE = "done"
RESOLVE_CONFLICTS = "resolve_conflicts"
ADDRESS_REVIEW = "address_review"
RESOLVE_COMMENTS = "resolve_comments"
FIX_CI = "fix_ci"
WAIT_CI = "wait_ci"
PUBLISH = "publish"
BLOCKED = "blocked"
MERGE = "merge"
WAIT_REVIEW = "wait_review"
REQUEST_REVIEW = "request_review"
KINDS = (
    OPEN_PR,
    DONE,
    RESOLVE_CONFLICTS,
    ADDRESS_REVIEW,
    RESOLVE_COMMENTS,
    FIX_CI,
    WAIT_CI,
    PUBLISH,
    BLOCKED,
    MERGE,
    WAIT_REVIEW,
    REQUEST_REVIEW,
)

#: ``KINDS`` as a type, for the API's wire models (a test keeps the two in
#: step).
type NextKind = Literal[
    "open_pr",
    "done",
    "resolve_conflicts",
    "address_review",
    "resolve_comments",
    "fix_ci",
    "wait_ci",
    "publish",
    "blocked",
    "merge",
    "wait_review",
    "request_review",
]


@dataclass(frozen=True)
class NextAction:
    label: str
    #: Who is blocking progress: ``you``, ``others``, ``ci`` or ``none``.
    who: str
    #: A stable identifier for the rule that fired (for styling and tests).
    kind: str


@dataclass(frozen=True)
class ParentContext:
    """The tracked branch this one is stacked on. ``pr`` is its PR state."""

    name: str
    pr: PrState
    #: Who the parent's own next action waits on (``WHO_*``).
    next_who: str = WHO_OTHERS


def next_action(status: BranchStatus, parent: ParentContext | None) -> NextAction:
    """``parent`` is ``None`` for a branch on the base branch (or whose
    parent's status is not known yet)."""
    if status.pr is PrState.NONE:
        return NextAction("Open PR", WHO_YOU, OPEN_PR)
    if status.pr in (PrState.MERGED, PrState.CLOSED):
        return NextAction("Done", WHO_NONE, DONE)
    if status.has_conflicts:
        return NextAction("Resolve conflicts", WHO_YOU, RESOLVE_CONFLICTS)
    if status.review is ReviewState.REVIEWED:
        return NextAction("Address review", WHO_YOU, ADDRESS_REVIEW)
    if status.open_comments > 0:
        return NextAction("Resolve comments", WHO_YOU, RESOLVE_COMMENTS)
    if status.ci is CiState.FAILED:
        return NextAction("Fix / re-run CI", WHO_YOU, FIX_CI)
    if status.ci is CiState.RUNNING:
        return NextAction("Waiting on CI", WHO_CI, WAIT_CI)
    if status.pr is PrState.DRAFT:
        return NextAction("Publish PR", WHO_YOU, PUBLISH)
    if status.review is ReviewState.APPROVED:
        if parent is not None and parent.pr is not PrState.MERGED:
            who = WHO_YOU if parent.next_who == WHO_YOU else WHO_OTHERS
            return NextAction(f"Blocked on {parent.name}", who, BLOCKED)
        return NextAction("Merge", WHO_YOU, MERGE)
    if status.review is ReviewState.REQUESTED:
        return NextAction("Waiting for review", WHO_OTHERS, WAIT_REVIEW)
    return NextAction("Request review", WHO_YOU, REQUEST_REVIEW)
