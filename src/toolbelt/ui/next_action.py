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
open, not approved,       Post in Slack              you
not posted in Slack
open, not approved,       Bump in Slack              you
posted, nobody looked
for a day
draft, CI running         Waiting on CI              ci
draft (CI green / none)   Publish PR in GitHub       you
open, CI running          Waiting on CI              ci
approved, parent PR not   Blocked on <parent>        you if the parent's own
merged (stacked)                                     next action is yours,
                                                     else others
approved                  Merge                      you
review requested          Waiting for review         others
otherwise                 Request review             you
========================  =========================  ======

The two Slack rows apply only when Slack is configured (``slack`` is given):
posting a ready PR in the review channel is how a review gets asked for. They
come after CI failed so an agent still sees a failing CI first, and before CI
running so a PR is posted while its checks run.

A branch with a question an agent asked the user (``deciding=True``) reads
**Deciding**, whatever else holds it: the agent cannot go on until the user
answers. The action it replaced travels with it as ``would_be``.

A branch an agent has claimed (``claimed=True``: a live ``working`` holder in
the claims directory, see ``task_claims``) reads **With an agent** instead,
whatever the table says, except when its PR is merged/closed (Done: the claim
is ignored). The action it replaced travels with it as ``would_be``. A branch
one of toolbelt's own autonomous agents holds (``autonomous=True``) reads
**Autonomous agent** the same way, unless an interactive agent or a person has
claimed it too: whoever is working at the keyboard takes precedence. A child
whose parent reads either is "Blocked on <parent>" waiting on ``others``: the
agent has it, not you.

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
#: An agent session has claimed the task and is working on it.
WHO_AGENT = "agent"
#: One of toolbelt's own autonomous agents holds the task.
WHO_AUTO = "auto"

#: Who-values in the order "needs me first" sorts them.
WHO_ORDER = [WHO_YOU, WHO_AGENT, WHO_AUTO, WHO_CI, WHO_OTHERS, WHO_NONE]

#: The values above as a type, for the API's wire models (a test keeps the
#: two in step).
type Who = Literal["you", "agent", "auto", "others", "ci", "none"]


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
POST_SLACK = "post_slack"
BUMP_SLACK = "bump_slack"
CLAIMED = "claimed"
AUTONOMOUS = "autonomous"
DECIDING = "deciding"
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
    POST_SLACK,
    BUMP_SLACK,
    CLAIMED,
    AUTONOMOUS,
    DECIDING,
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
    "post_slack",
    "bump_slack",
    "claimed",
    "autonomous",
    "deciding",
]


@dataclass(frozen=True)
class NextAction:
    label: str
    #: Who is blocking progress: ``you``, ``agent``, ``others``, ``ci`` or
    #: ``none``.
    who: str
    #: A stable identifier for the rule that fired (for styling and tests).
    kind: str
    #: For "With an agent": the action that would show without the claim.
    would_be: NextAction | None = None


@dataclass(frozen=True)
class SlackContext:
    """Where the PR stands in Slack (see ``slack_posts``). Not given when
    Slack is not configured."""

    posted: bool
    #: Posted over a day ago and nobody has looked since.
    bump_due: bool = False


@dataclass(frozen=True)
class ParentContext:
    """The tracked branch this one is stacked on. ``pr`` is its PR state."""

    name: str
    pr: PrState
    #: Who the parent's own next action waits on (``WHO_*``).
    next_who: str = WHO_OTHERS


def next_action(
    status: BranchStatus,
    parent: ParentContext | None,
    claimed: bool = False,
    autonomous: bool = False,
    slack: SlackContext | None = None,
    deciding: bool = False,
) -> NextAction:
    """``parent`` is ``None`` for a branch on the base branch (or whose
    parent's status is not known yet). ``claimed``: a live working holder has
    claimed the branch; ``autonomous``: a live working autonomous agent
    holds it. ``slack``: where the PR stands in Slack (``None``: Slack is not
    configured, so it plays no part). ``deciding``: an agent asked the user a
    question about the task that is not answered yet: nothing can go on until
    it is, so it comes before whoever holds the task."""
    underlying = _underlying_action(status, parent, slack)
    if underlying.kind == DONE:
        return underlying
    if deciding:
        return NextAction("Deciding", WHO_YOU, DECIDING, would_be=underlying)
    if claimed:
        return NextAction("With an agent", WHO_AGENT, CLAIMED, would_be=underlying)
    if autonomous:
        return NextAction("Autonomous agent", WHO_AUTO, AUTONOMOUS, would_be=underlying)
    return underlying


def _underlying_action(
    status: BranchStatus, parent: ParentContext | None, slack: SlackContext | None
) -> NextAction:
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
    if (
        slack is not None
        and status.pr is PrState.OPEN
        and status.review is not ReviewState.APPROVED
    ):
        if not slack.posted:
            return NextAction("Post in Slack", WHO_YOU, POST_SLACK)
        if slack.bump_due:
            return NextAction("Bump in Slack", WHO_YOU, BUMP_SLACK)
    if status.ci is CiState.RUNNING:
        return NextAction("Waiting on CI", WHO_CI, WAIT_CI)
    if status.pr is PrState.DRAFT:
        return NextAction("Publish PR in GitHub", WHO_YOU, PUBLISH)
    if status.review is ReviewState.APPROVED:
        if parent is not None and parent.pr is not PrState.MERGED:
            who = WHO_YOU if parent.next_who == WHO_YOU else WHO_OTHERS
            return NextAction(f"Blocked on {parent.name}", who, BLOCKED)
        return NextAction("Merge", WHO_YOU, MERGE)
    if status.review is ReviewState.REQUESTED:
        return NextAction("Waiting for review", WHO_OTHERS, WAIT_REVIEW)
    return NextAction("Request review", WHO_YOU, REQUEST_REVIEW)
