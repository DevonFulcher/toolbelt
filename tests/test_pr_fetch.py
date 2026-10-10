"""The UI's single-call PR lookup: GraphQL response -> status + facts.

The `gh` call is the injected ``graphql`` runner, so these tests feed canned
responses and count calls; nothing reaches GitHub.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from toolbelt.git.stack.status import CiState, PrState, ReviewState
from toolbelt.ui import pr_activity as pa
from toolbelt.ui.pr_fetch import NO_SNAPSHOT, PR_QUERY, fetch_snapshot, parse_snapshot

ROOT = Path("/repo")


def pr_node(**overrides) -> dict:
    node = {
        "number": 42,
        "url": "https://github.com/o/r/pull/42",
        "title": "Add the thing",
        "state": "OPEN",
        "isDraft": False,
        "createdAt": "2026-10-01T09:00:00Z",
        "mergedAt": None,
        "closedAt": None,
        "headRefOid": "c3c3c3c",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "author": {"login": "devon"},
        "reviewRequests": {"nodes": []},
        "reviewThreads": {"nodes": []},
        "head": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "state": "SUCCESS",
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "conclusion": "SUCCESS",
                                        "status": "COMPLETED",
                                    }
                                ]
                            },
                        }
                    }
                }
            ]
        },
        "recent": {"nodes": []},
        "timelineItems": {"nodes": []},
    }
    node.update(overrides)
    return node


def response(*nodes: dict) -> dict:
    return {"data": {"repository": {"pullRequests": {"nodes": list(nodes)}}}}


class Graphql:
    """A ``GraphqlRunner`` replaying canned responses, recording its calls."""

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, Path]] = []

    async def __call__(self, branch: str, root: Path) -> dict:
        self.calls.append((branch, root))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


async def no_sleep(_seconds: float) -> None:
    return None


def fetch(graphql: Graphql):
    return asyncio.run(fetch_snapshot("devon/a", ROOT, graphql=graphql, sleep=no_sleep))


def test_no_pr_is_one_call_and_an_empty_snapshot():
    graphql = Graphql(response())

    snapshot = fetch(graphql)

    assert snapshot == NO_SNAPSHOT
    assert graphql.calls == [("devon/a", ROOT)]


def test_one_call_returns_the_status_the_table_shows():
    node = pr_node(
        reviewDecision="APPROVED",
        reviewThreads={"nodes": [{"isResolved": False}, {"isResolved": True}]},
        mergeable="CONFLICTING",
    )
    graphql = Graphql(response(node))

    snapshot = fetch(graphql)

    assert len(graphql.calls) == 1
    status = snapshot.status
    assert status.pr is PrState.OPEN
    assert status.ci is CiState.SUCCESS
    assert status.review is ReviewState.APPROVED
    assert status.url == "https://github.com/o/r/pull/42"
    assert status.open_comments == 1
    assert status.has_conflicts


@pytest.mark.parametrize(
    ("contexts", "expected"),
    [
        ([{"conclusion": "FAILURE", "status": "COMPLETED"}], CiState.FAILED),
        ([{"conclusion": None, "status": "IN_PROGRESS"}], CiState.RUNNING),
        ([{"state": "ERROR"}], CiState.FAILED),
        ([{"state": "PENDING"}], CiState.RUNNING),
        (
            [{"conclusion": "SUCCESS", "status": "COMPLETED"}, {"state": "SUCCESS"}],
            CiState.SUCCESS,
        ),
    ],
)
def test_ci_state_comes_from_the_head_commits_check_contexts(contexts, expected):
    node = pr_node(
        head={
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "state": "X",
                            "contexts": {
                                "nodes": [{"__typename": "x", **c} for c in contexts]
                            },
                        }
                    }
                }
            ]
        }
    )

    assert parse_snapshot(response(node)).status.ci is expected


def test_no_checks_is_no_ci():
    node = pr_node(head={"nodes": [{"commit": {"statusCheckRollup": None}}]})

    assert parse_snapshot(response(node)).status.ci is CiState.NONE


def test_requested_reviewers_may_be_users_or_teams():
    node = pr_node(
        reviewRequests={
            "nodes": [
                {"requestedReviewer": {"__typename": "User", "login": "sam"}},
                {"requestedReviewer": {"__typename": "Team", "name": "core"}},
            ]
        }
    )

    snapshot = parse_snapshot(response(node))

    assert snapshot.status.review is ReviewState.REQUESTED
    assert snapshot.facts is not None
    assert snapshot.facts.requested_reviewers == ("sam", "core")


def test_facts_carry_the_pr_and_its_timeline_in_time_order():
    node = pr_node(
        state="MERGED",
        mergedAt="2026-10-03T12:00:00Z",
        timelineItems={
            "nodes": [
                {
                    "__typename": "MergedEvent",
                    "createdAt": "2026-10-03T12:00:00Z",
                    "actor": {"login": "devon"},
                },
                {
                    "__typename": "PullRequestCommit",
                    "commit": {
                        "oid": "a1a1a1a2b2",
                        "messageHeadline": "Add it",
                        "committedDate": "2026-10-01T09:30:00Z",
                        "author": {"user": {"login": "devon"}, "name": "Devon"},
                        "statusCheckRollup": {"state": "FAILURE"},
                    },
                },
                {
                    "__typename": "PullRequestReview",
                    "submittedAt": "2026-10-02T08:00:00Z",
                    "state": "APPROVED",
                    "author": {"login": "sam"},
                },
                {
                    "__typename": "PullRequestReview",
                    "submittedAt": None,  # a draft review nobody has seen
                    "state": "PENDING",
                    "author": {"login": "lee"},
                },
                {
                    "__typename": "HeadRefForcePushedEvent",
                    "createdAt": "2026-10-01T10:00:00Z",
                    "actor": {"login": "devon"},
                    "beforeCommit": {
                        "oid": "dead0000",
                        "statusCheckRollup": {"state": "FAILURE"},
                    },
                    "afterCommit": {"oid": "beef1111"},
                },
            ]
        },
        recent={
            "nodes": [
                {
                    "commit": {
                        "oid": "c3c3c3c",
                        "statusCheckRollup": {"state": "SUCCESS"},
                    }
                },
                {"commit": {"oid": "d4d4d4d", "statusCheckRollup": None}},
            ]
        },
    )

    facts = parse_snapshot(response(node)).facts

    assert facts is not None
    assert (facts.number, facts.title, facts.author) == (42, "Add the thing", "devon")
    assert facts.state == "MERGED"
    assert facts.created_at == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert facts.merged_at == datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    assert [(i.kind, i.actor) for i in facts.items] == [
        (pa.COMMIT, "devon"),
        (pa.FORCE_PUSH, "devon"),
        (pa.REVIEW, "sam"),
        (pa.MERGED, "devon"),
    ]
    commit, push, review, _ = facts.items
    assert commit.detail == "a1a1a1a Add it"
    assert (commit.oid, commit.ci) == ("a1a1a1a2b2", "FAILURE")
    assert (push.oid, push.ci) == ("dead0000", "FAILURE")
    assert review.detail == "approved"
    assert facts.commit_rollups == (("c3c3c3c", "SUCCESS"), ("d4d4d4d", None))


def test_review_request_events_name_the_reviewer():
    node = pr_node(
        timelineItems={
            "nodes": [
                {
                    "__typename": "ReviewRequestedEvent",
                    "createdAt": "2026-10-01T10:00:00Z",
                    "actor": {"login": "devon"},
                    "requestedReviewer": {"__typename": "User", "login": "sam"},
                },
                {
                    "__typename": "ReviewRequestRemovedEvent",
                    "createdAt": "2026-10-01T11:00:00Z",
                    "actor": {"login": "devon"},
                    "requestedReviewer": {"__typename": "Team", "name": "core"},
                },
                {
                    "__typename": "ReadyForReviewEvent",
                    "createdAt": "2026-10-01T12:00:00Z",
                    "actor": {"login": "devon"},
                },
                {
                    "__typename": "ConvertToDraftEvent",
                    "createdAt": "2026-10-01T13:00:00Z",
                    "actor": {"login": "devon"},
                },
                {
                    "__typename": "ClosedEvent",
                    "createdAt": "2026-10-01T14:00:00Z",
                    "actor": {"login": "devon"},
                },
                {
                    "__typename": "ReopenedEvent",
                    "createdAt": "2026-10-01T15:00:00Z",
                    "actor": {"login": "devon"},
                },
            ]
        }
    )

    facts = parse_snapshot(response(node)).facts

    assert facts is not None
    assert [(i.kind, i.subject) for i in facts.items] == [
        (pa.REVIEW_REQUESTED, "sam"),
        (pa.REVIEW_REQUEST_REMOVED, "core"),
        (pa.READY_FOR_REVIEW, ""),
        (pa.CONVERTED_TO_DRAFT, ""),
        (pa.CLOSED, ""),
        (pa.REOPENED, ""),
    ]


def test_mergeable_unknown_is_retried_until_it_settles():
    graphql = Graphql(
        response(pr_node(mergeable="UNKNOWN")),
        response(pr_node(mergeable="UNKNOWN")),
        response(pr_node(mergeable="CONFLICTING")),
    )

    snapshot = fetch(graphql)

    assert len(graphql.calls) == 3
    assert snapshot.status.has_conflicts


def test_a_merged_pr_is_not_retried_for_mergeable():
    graphql = Graphql(response(pr_node(state="MERGED", mergeable="UNKNOWN")))

    fetch(graphql)

    assert len(graphql.calls) == 1


def test_the_query_asks_for_everything_the_derivations_need():
    for needed in (
        "headRefName: $branch",
        "reviewThreads",
        "reviewRequests",
        "ReadyForReviewEvent",
        "ConvertToDraftEvent",
        "ReviewRequestedEvent",
        "ReviewRequestRemovedEvent",
        "PullRequestReview",
        "PullRequestCommit",
        "HeadRefForcePushedEvent",
        "MergedEvent",
        "ClosedEvent",
        "ReopenedEvent",
        "statusCheckRollup",
        "mergeable",
        "headRefOid",
        "baseRefName",
    ):
        assert needed in PR_QUERY, needed


def test_responses_are_plain_json_round_trippable():
    # Guards the fixtures themselves: the runner returns parsed JSON.
    assert json.loads(json.dumps(response(pr_node())))
