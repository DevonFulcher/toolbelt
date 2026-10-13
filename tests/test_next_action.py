"""`next_action`: one row per rule, plus the priority ties between rules."""

import pytest

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.ui.next_action import ParentContext, next_action

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
            "Publish PR",
            "you",
            "publish",
        ),
        (
            "draft, no CI",
            status(pr=PrState.DRAFT, ci=CiState.NONE),
            None,
            "Publish PR",
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
