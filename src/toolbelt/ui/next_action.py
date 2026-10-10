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
approved, parent PR not   Blocked on <parent>        others
merged (stacked)
approved                  Merge                      you
review requested          Waiting for review         others
otherwise                 Request review             you
========================  =========================  ======

"Blocked on <parent>" replaces "Merge" only: a child's review can proceed
while its parent is open, but it must not be merged before the parent.
"""

from dataclasses import dataclass

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState

WHO_YOU = "you"
WHO_OTHERS = "others"
WHO_CI = "ci"
WHO_NONE = "none"

#: Who-values in the order "needs me first" sorts them.
WHO_ORDER = [WHO_YOU, WHO_CI, WHO_OTHERS, WHO_NONE]


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


def next_action(status: BranchStatus, parent: ParentContext | None) -> NextAction:
    """``parent`` is ``None`` for a branch on the base branch (or whose
    parent's status is not known yet)."""
    if status.pr is PrState.NONE:
        return NextAction("Open PR", WHO_YOU, "open_pr")
    if status.pr in (PrState.MERGED, PrState.CLOSED):
        return NextAction("Done", WHO_NONE, "done")
    if status.has_conflicts:
        return NextAction("Resolve conflicts", WHO_YOU, "resolve_conflicts")
    if status.review is ReviewState.REVIEWED:
        return NextAction("Address review", WHO_YOU, "address_review")
    if status.open_comments > 0:
        return NextAction("Resolve comments", WHO_YOU, "resolve_comments")
    if status.ci is CiState.FAILED:
        return NextAction("Fix / re-run CI", WHO_YOU, "fix_ci")
    if status.ci is CiState.RUNNING:
        return NextAction("Waiting on CI", WHO_CI, "wait_ci")
    if status.pr is PrState.DRAFT:
        return NextAction("Publish PR", WHO_YOU, "publish")
    if status.review is ReviewState.APPROVED:
        if parent is not None and parent.pr is not PrState.MERGED:
            return NextAction(f"Blocked on {parent.name}", WHO_OTHERS, "blocked")
        return NextAction("Merge", WHO_YOU, "merge")
    if status.review is ReviewState.REQUESTED:
        return NextAction("Waiting for review", WHO_OTHERS, "wait_review")
    return NextAction("Request review", WHO_YOU, "request_review")
