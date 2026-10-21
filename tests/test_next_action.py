"""`next_action`: one row per rule, plus the priority ties between rules."""

import pytest

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.ui.next_action import ParentContext, SlackContext, next_action

URL = "https://github.com/o/r/pull/5"


def status(
    pr=PrState.OPEN,
    ci=CiState.SUCCESS,
    review=ReviewState.NONE,
    comments=0,
    conflicts=False,
) -> BranchStatus:
    return BranchStatus(
        pr=pr,
        ci=ci,
        review=review,
        url=None if pr is PrState.NONE else URL,
        open_comments=comments,
        has_conflicts=conflicts,
    )


MERGED_PARENT = ParentContext("devon/base", PrState.MERGED)
OPEN_PARENT = ParentContext("devon/base", PrState.OPEN)
# A parent whose own next action is the user's (e.g. fix CI), or waits on
# someone else / CI.
MINE_PARENT = ParentContext("devon/base", PrState.OPEN, next_who="you")
CI_PARENT = ParentContext("devon/base", PrState.OPEN, next_who="ci")
OTHERS_PARENT = ParentContext("devon/base", PrState.OPEN, next_who="others")


@pytest.mark.parametrize(
    ("name", "branch", "parent", "label", "who", "kind"),
    [
        # one row per rule
        (
            "no PR",
            status(pr=PrState.NONE, ci=CiState.NONE),
            None,
            "Open PR",
            "you",
            "open_pr",
        ),
        ("merged", status(pr=PrState.MERGED), None, "Done", "none", "done"),
        ("closed", status(pr=PrState.CLOSED), None, "Done", "none", "done"),
        (
            "conflicts",
            status(conflicts=True),
            None,
            "Resolve conflicts",
            "you",
            "resolve_conflicts",
        ),
        (
            "changes requested",
            status(review=ReviewState.REVIEWED),
            None,
            "Address review",
            "you",
            "address_review",
        ),
        (
            "unresolved threads",
            status(comments=2),
            None,
            "Resolve comments",
            "you",
            "resolve_comments",
        ),
        (
            "CI failed",
            status(ci=CiState.FAILED),
            None,
            "Fix / re-run CI",
            "you",
            "fix_ci",
        ),
        (
            "draft, CI green",
            status(pr=PrState.DRAFT),
            None,
            "Publish PR in GitHub",
            "you",
            "publish",
        ),
        (
            "draft, no CI",
            status(pr=PrState.DRAFT, ci=CiState.NONE),
            None,
            "Publish PR in GitHub",
            "you",
            "publish",
        ),
        (
            "draft, CI running",
            status(pr=PrState.DRAFT, ci=CiState.RUNNING),
            None,
            "Waiting on CI",
            "ci",
            "wait_ci",
        ),
        (
            "open, CI running",
            status(ci=CiState.RUNNING),
            None,
            "Waiting on CI",
            "ci",
            "wait_ci",
        ),
        (
            "open, green, nobody asked",
            status(),
            None,
            "Request review",
            "you",
            "request_review",
        ),
        (
            "open, no CI configured, nobody asked",
            status(ci=CiState.NONE),
            None,
            "Request review",
            "you",
            "request_review",
        ),
        (
            "review requested",
            status(review=ReviewState.REQUESTED),
            None,
            "Waiting for review",
            "others",
            "wait_review",
        ),
        (
            "approved",
            status(review=ReviewState.APPROVED),
            None,
            "Merge",
            "you",
            "merge",
        ),
        (
            "approved, no CI configured",
            status(ci=CiState.NONE, review=ReviewState.APPROVED),
            None,
            "Merge",
            "you",
            "merge",
        ),
        (
            "approved on a merged parent",
            status(review=ReviewState.APPROVED),
            MERGED_PARENT,
            "Merge",
            "you",
            "merge",
        ),
        (
            "approved on an open parent",
            status(review=ReviewState.APPROVED),
            OPEN_PARENT,
            "Blocked on devon/base",
            "others",
            "blocked",
        ),
        (
            "approved on a parent that needs me: the stack is my work",
            status(review=ReviewState.APPROVED),
            MINE_PARENT,
            "Blocked on devon/base",
            "you",
            "blocked",
        ),
        (
            "approved on a parent waiting on CI",
            status(review=ReviewState.APPROVED),
            CI_PARENT,
            "Blocked on devon/base",
            "others",
            "blocked",
        ),
        (
            "approved on a parent waiting on others",
            status(review=ReviewState.APPROVED),
            OTHERS_PARENT,
            "Blocked on devon/base",
            "others",
            "blocked",
        ),
        (
            "approved on a parent without a PR",
            status(review=ReviewState.APPROVED),
            ParentContext("devon/base", PrState.NONE),
            "Blocked on devon/base",
            "others",
            "blocked",
        ),
        # priority: the first rule that applies wins
        (
            "conflicts beat changes requested",
            status(conflicts=True, review=ReviewState.REVIEWED, comments=1),
            None,
            "Resolve conflicts",
            "you",
            "resolve_conflicts",
        ),
        (
            "changes requested beat comments and failing CI",
            status(review=ReviewState.REVIEWED, comments=1, ci=CiState.FAILED),
            None,
            "Address review",
            "you",
            "address_review",
        ),
        (
            "comments beat failing CI",
            status(comments=1, ci=CiState.FAILED),
            None,
            "Resolve comments",
            "you",
            "resolve_comments",
        ),
        (
            "failing CI beats a draft being published",
            status(pr=PrState.DRAFT, ci=CiState.FAILED),
            None,
            "Fix / re-run CI",
            "you",
            "fix_ci",
        ),
        (
            "CI running beats approval (don't merge a red-to-be)",
            status(ci=CiState.RUNNING, review=ReviewState.APPROVED),
            None,
            "Waiting on CI",
            "ci",
            "wait_ci",
        ),
        (
            "approved beats a still-pending request",
            status(review=ReviewState.APPROVED),
            None,
            "Merge",
            "you",
            "merge",
        ),
        (
            "an open parent does not hide your own blockers",
            status(ci=CiState.FAILED, review=ReviewState.APPROVED),
            OPEN_PARENT,
            "Fix / re-run CI",
            "you",
            "fix_ci",
        ),
        (
            "an open parent does not block asking for review",
            status(),
            OPEN_PARENT,
            "Request review",
            "you",
            "request_review",
        ),
        (
            "a merged PR is done whatever else is set",
            status(pr=PrState.MERGED, ci=CiState.FAILED, comments=3, conflicts=True),
            OPEN_PARENT,
            "Done",
            "none",
            "done",
        ),
    ],
)
def test_next_action(name, branch, parent, label, who, kind):
    action = next_action(branch, parent)

    assert (action.label, action.who, action.kind) == (label, who, kind), name


CLAIMED = ("With an agent", "agent", "claimed")


@pytest.mark.parametrize(
    ("name", "branch", "underlying"),
    [
        ("no PR yet", status(pr=PrState.NONE, ci=CiState.NONE), "Open PR"),
        ("conflicts", status(conflicts=True), "Resolve conflicts"),
        ("open comments", status(comments=2), "Resolve comments"),
        ("failing CI", status(ci=CiState.FAILED), "Fix / re-run CI"),
        ("CI running", status(ci=CiState.RUNNING), "Waiting on CI"),
        ("approved", status(review=ReviewState.APPROVED), "Merge"),
        (
            "waiting for review",
            status(review=ReviewState.REQUESTED),
            "Waiting for review",
        ),
    ],
)
def test_a_claim_replaces_the_action_and_keeps_it_as_would_be(name, branch, underlying):
    action = next_action(branch, None, claimed=True)

    assert (action.label, action.who, action.kind) == CLAIMED, name
    assert action.would_be == next_action(branch, None)
    assert action.would_be.label == underlying


@pytest.mark.parametrize("pr", [PrState.MERGED, PrState.CLOSED])
def test_a_claim_on_a_finished_pr_is_ignored(pr):
    action = next_action(status(pr=pr), None, claimed=True)

    assert (action.label, action.who, action.kind) == ("Done", "none", "done")
    assert action.would_be is None


def test_no_claim_means_no_would_be():
    assert next_action(status(), None).would_be is None


def test_a_child_of_an_agents_branch_waits_on_others_not_on_you():
    agent_parent = ParentContext("devon/base", PrState.OPEN, next_who="agent")

    action = next_action(status(review=ReviewState.APPROVED), agent_parent)

    assert (action.label, action.who, action.kind) == (
        "Blocked on devon/base",
        "others",
        "blocked",
    )


def test_a_claimed_child_still_reads_with_an_agent_not_blocked():
    action = next_action(status(review=ReviewState.APPROVED), OPEN_PARENT, claimed=True)

    assert action.kind == "claimed"
    assert action.would_be.kind == "blocked"


AUTONOMOUS = ("Autonomous agent", "auto", "autonomous")


def test_an_autonomous_agent_replaces_the_action_and_keeps_it_as_would_be():
    branch = status(ci=CiState.FAILED)

    action = next_action(branch, None, autonomous=True)

    assert (action.label, action.who, action.kind) == AUTONOMOUS
    assert action.would_be == next_action(branch, None)
    assert action.would_be.label == "Fix / re-run CI"


def test_an_interactive_claim_takes_precedence_over_an_autonomous_agent():
    action = next_action(status(), None, claimed=True, autonomous=True)

    assert (action.label, action.who, action.kind) == CLAIMED


@pytest.mark.parametrize("pr", [PrState.MERGED, PrState.CLOSED])
def test_an_autonomous_agent_on_a_finished_pr_is_ignored(pr):
    action = next_action(status(pr=pr), None, autonomous=True)

    assert action.kind == "done"


def test_a_child_of_an_autonomous_agents_branch_waits_on_others():
    parent = ParentContext("devon/base", PrState.OPEN, next_who="auto")

    action = next_action(status(review=ReviewState.APPROVED), parent)

    assert (action.kind, action.who) == ("blocked", "others")


class TestSlackRules:
    """With Slack configured, a ready PR is posted, then bumped when nobody
    looks."""

    NOT_POSTED = SlackContext(posted=False)
    POSTED = SlackContext(posted=True)
    BUMP_DUE = SlackContext(posted=True, bump_due=True)

    def kind(self, branch, slack, parent=None):
        return next_action(branch, parent, slack=slack).kind

    @pytest.mark.parametrize(
        "branch",
        [
            status(),
            status(review=ReviewState.REQUESTED),
            status(ci=CiState.RUNNING),
            status(ci=CiState.NONE),
        ],
    )
    def test_a_ready_pr_not_posted_is_to_be_posted_whatever_its_review_request_or_running_ci(
        self, branch
    ):
        assert self.kind(branch, self.NOT_POSTED) == "post_slack"
        assert next_action(branch, None, slack=self.NOT_POSTED).label == "Post in Slack"

    def test_without_slack_nothing_changes(self):
        assert self.kind(status(), None) == "request_review"
        assert self.kind(status(review=ReviewState.REQUESTED), None) == "wait_review"
        assert self.kind(status(ci=CiState.RUNNING), None) == "wait_ci"

    def test_a_posted_pr_is_bumped_once_due_and_otherwise_waits_as_before(self):
        assert self.kind(status(), self.BUMP_DUE) == "bump_slack"
        assert self.kind(status(), self.POSTED) == "request_review"
        waiting = status(review=ReviewState.REQUESTED)
        assert self.kind(waiting, self.POSTED) == "wait_review"
        assert self.kind(status(ci=CiState.RUNNING), self.POSTED) == "wait_ci"

    @pytest.mark.parametrize(
        ("branch", "kind"),
        [
            (status(conflicts=True), "resolve_conflicts"),
            (status(review=ReviewState.REVIEWED), "address_review"),
            (status(comments=1), "resolve_comments"),
            # An agent must still see a failing CI first.
            (status(ci=CiState.FAILED), "fix_ci"),
        ],
    )
    def test_what_blocks_the_pr_comes_before_posting_it(self, branch, kind):
        assert self.kind(branch, self.NOT_POSTED) == kind
        assert self.kind(branch, self.BUMP_DUE) == kind

    @pytest.mark.parametrize(
        ("branch", "kind"),
        [
            (status(pr=PrState.NONE), "open_pr"),
            (status(pr=PrState.DRAFT), "publish"),
            (status(pr=PrState.DRAFT, ci=CiState.RUNNING), "wait_ci"),
            (status(pr=PrState.MERGED), "done"),
            (status(pr=PrState.CLOSED), "done"),
            (status(review=ReviewState.APPROVED), "merge"),
        ],
    )
    def test_a_draft_a_finished_pr_and_an_approved_one_are_not_posted(
        self, branch, kind
    ):
        assert self.kind(branch, self.NOT_POSTED) == kind
        assert self.kind(branch, self.BUMP_DUE) == kind

    def test_an_approved_child_is_still_blocked_on_its_parent(self):
        approved = status(review=ReviewState.APPROVED)

        assert self.kind(approved, self.NOT_POSTED, OPEN_PARENT) == "blocked"

    def test_an_agent_claim_still_replaces_the_slack_action(self):
        action = next_action(status(), None, claimed=True, slack=self.NOT_POSTED)

        assert action.kind == "claimed"
        assert action.would_be is not None and action.would_be.kind == "post_slack"
