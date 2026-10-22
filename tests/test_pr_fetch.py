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
        "additions": 120,
        "deletions": 4,
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


def _thread(*, resolved: bool, typename: str, login: str) -> dict:
    return {
        "isResolved": resolved,
        "comments": {"nodes": [{"author": {"__typename": typename, "login": login}}]},
    }


def test_unresolved_threads_a_bot_started_are_counted_apart_from_a_persons():
    node = pr_node(
        reviewThreads={
            "nodes": [
                _thread(
                    resolved=False,
                    typename="Bot",
                    login="copilot-pull-request-reviewer",
                ),
                _thread(resolved=False, typename="User", login="sam"),
                _thread(resolved=False, typename="User", login="Copilot"),
                _thread(
                    resolved=True, typename="Bot", login="copilot-pull-request-reviewer"
                ),
                {"isResolved": False, "comments": {"nodes": []}},
            ]
        }
    )

    status = fetch(Graphql(response(node))).status

    assert status.open_comments == 4
    assert status.bot_comments == 2


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


def run_check(
    name, conclusion, *, workflow="PR pipeline", at="2026-10-22T02:48:36Z", run=1
):
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": "COMPLETED",
        "startedAt": at,
        "checkSuite": {
            "workflowRun": {
                "databaseId": run,
                "createdAt": at,
                "workflow": {"name": workflow},
            }
        },
    }


def ci_of(*contexts):
    node = pr_node(
        head={
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "state": "FAILURE",
                            "contexts": {"nodes": list(contexts)},
                        }
                    }
                }
            ]
        }
    )
    return parse_snapshot(response(node)).status.ci


def test_the_query_asks_for_what_the_dedupe_of_runs_reads():
    """``current_contexts`` reads each check run's workflow and its run's time:
    without them in the query nothing is superseded and old failures count."""
    from toolbelt.ui.pr_fetch import PR_QUERY

    for needed in ("workflowRun", "databaseId", "createdAt", "workflow {", "startedAt"):
        assert needed in PR_QUERY


def test_a_newer_run_of_a_workflow_supersedes_the_failures_of_its_earlier_runs():
    earlier = "2026-10-22T02:48:36Z"
    later = "2026-10-22T13:56:02Z"

    ci = ci_of(
        run_check("Integration tests status", "FAILURE", at=earlier, run=1),
        run_check("Coverage report", "CANCELLED", at=earlier, run=1),
        run_check("Integration tests status", "SUCCESS", at=later, run=2),
    )

    assert (
        ci is CiState.SUCCESS
    )  # the cancelled check has no newer one, but its run does


def test_a_failure_in_the_newest_run_still_fails():
    ci = ci_of(
        run_check("build", "SUCCESS", at="2026-10-22T02:48:36Z", run=1),
        run_check("build", "FAILURE", at="2026-10-22T13:56:02Z", run=2),
    )

    assert ci is CiState.FAILED


def test_a_workflows_runs_are_judged_apart_from_other_workflows():
    ci = ci_of(
        run_check("lint", "FAILURE", workflow="Lint", run=1),
        run_check(
            "build", "SUCCESS", workflow="PR pipeline", run=2, at="2026-10-22T13:56:02Z"
        ),
    )

    assert ci is CiState.FAILED


def test_a_newer_run_in_progress_replaces_an_earlier_failure():
    running = {
        **run_check("build", None, at="2026-10-22T13:56:02Z", run=2),
        "status": "IN_PROGRESS",
    }

    ci = ci_of(run_check("build", "FAILURE", run=1), running)

    assert ci is CiState.RUNNING


def test_every_job_of_the_newest_run_counts_even_when_they_share_a_name():
    at = "2026-10-22T13:56:02Z"

    ci = ci_of(
        run_check("Integration tests", "SUCCESS", at=at, run=2),
        run_check("Integration tests", "FAILURE", at=at, run=2),
    )

    assert ci is CiState.FAILED


def test_a_check_run_with_no_workflow_counts_once_the_newest_by_name():
    older = {
        "__typename": "CheckRun",
        "name": "x",
        "conclusion": "FAILURE",
        "status": "COMPLETED",
        "startedAt": "2026-10-22T01:00:00Z",
        "checkSuite": {"workflowRun": None},
    }
    newer = {**older, "conclusion": "SUCCESS", "startedAt": "2026-10-22T02:00:00Z"}

    assert ci_of(older, newer) is CiState.SUCCESS
    assert ci_of(newer, older) is CiState.SUCCESS


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
    assert (facts.additions, facts.deletions) == (120, 4)
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


def test_a_commits_completion_time_is_its_latest_completed_suite():
    def commit_with(suites):
        return pr_node(
            timelineItems={
                "nodes": [
                    {
                        "__typename": "PullRequestCommit",
                        "commit": {
                            "oid": "a1a1a1a2b2",
                            "messageHeadline": "Add it",
                            "committedDate": "2026-10-01T09:30:00Z",
                            "author": {"user": {"login": "devon"}, "name": "Devon"},
                            "statusCheckRollup": {"state": "FAILURE"},
                            "checkSuites": {"nodes": suites},
                        },
                    }
                ]
            }
        )

    def done(suites):
        payload = {
            "data": {"repository": {"pullRequests": {"nodes": [commit_with(suites)]}}}
        }
        facts = parse_snapshot(payload).facts
        assert facts is not None
        return facts.items[0].ci_done

    finished = [
        {"status": "COMPLETED", "updatedAt": "2026-10-01T09:40:00Z"},
        {"status": "COMPLETED", "updatedAt": "2026-10-01T09:55:00Z"},
    ]
    assert done(finished) == datetime(2026, 10, 1, 9, 55, tzinfo=UTC)
    # One suite still running: not complete yet. And no suites: unknown.
    assert done([*finished, {"status": "IN_PROGRESS", "updatedAt": "x"}]) is None
    assert done([]) is None


def test_the_query_asks_for_everything_the_derivations_need():
    assert "checkSuites" in PR_QUERY
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


# ---- shapes seen in a real GitHub response (validation fixture repo) ---------


def test_a_merge_also_records_a_closed_event_which_is_not_a_second_ending():
    # Real payload shape: MergedEvent and ClosedEvent at the same second.
    node = pr_node(
        state="MERGED",
        mergedAt="2026-10-01T09:00:56Z",
        closedAt="2026-10-01T09:00:56Z",
        mergeable="UNKNOWN",
        head={"nodes": [{"commit": {"statusCheckRollup": None}}]},
        timelineItems={
            "nodes": [
                {
                    "__typename": "PullRequestCommit",
                    "commit": {
                        "oid": "07d9986",
                        "messageHeadline": "add it",
                        "committedDate": "2026-10-01T09:00:49Z",
                        "author": {"user": {"login": "devon"}, "name": "Devon"},
                        "statusCheckRollup": None,
                    },
                },
                {
                    "__typename": "MergedEvent",
                    "createdAt": "2026-10-01T09:00:56Z",
                    "actor": {"login": "devon"},
                },
                {
                    "__typename": "ClosedEvent",
                    "createdAt": "2026-10-01T09:00:56Z",
                    "actor": {"login": "devon"},
                },
            ]
        },
    )

    snapshot = parse_snapshot(response(node))

    assert snapshot.facts is not None
    assert [item.kind for item in snapshot.facts.items] == [pa.COMMIT, pa.MERGED]
    assert snapshot.status.pr is PrState.MERGED


def test_a_pr_closed_and_later_merged_keeps_its_real_close():
    node = pr_node(
        state="MERGED",
        timelineItems={
            "nodes": [
                {
                    "__typename": "ClosedEvent",
                    "createdAt": "2026-10-02T09:00:00Z",
                    "actor": {"login": "devon"},
                },
                {
                    "__typename": "ReopenedEvent",
                    "createdAt": "2026-10-03T09:00:00Z",
                    "actor": {"login": "devon"},
                },
                {
                    "__typename": "MergedEvent",
                    "createdAt": "2026-10-04T09:00:00Z",
                    "actor": {"login": "devon"},
                },
                {
                    "__typename": "ClosedEvent",
                    "createdAt": "2026-10-04T09:00:00Z",
                    "actor": {"login": "devon"},
                },
            ]
        },
    )

    facts = parse_snapshot(response(node)).facts

    assert facts is not None
    assert [item.kind for item in facts.items] == [pa.CLOSED, pa.REOPENED, pa.MERGED]
