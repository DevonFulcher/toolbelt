"""Why a PR's CI keeps failing, said in a sentence: which checks fail, whether
the failures persist or come and go, and how many commits are merges of other
branches. The history is built the way GitHub answers (no network)."""

import asyncio

from toolbelt.autonomous_agents.ci_history import (
    CI_HISTORY_QUERY,
    CommitCi,
    ci_explainer,
    explain,
    parse_history,
)


def check_run(
    name, conclusion, *, run=1, at="2026-10-22T10:00:00Z", workflow="PR pipeline"
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


def node(oid, state, contexts=(), parents=1):
    return {
        "commit": {
            "oid": oid,
            "parents": {"totalCount": parents},
            "statusCheckRollup": {"state": state, "contexts": {"nodes": list(contexts)}}
            if state
            else None,
        }
    }


def history(*nodes):
    return {
        "data": {"repository": {"pullRequest": {"commits": {"nodes": list(nodes)}}}}
    }


def failed(oid, *names, merge=False):
    return CommitCi(oid, merge, "FAILURE", tuple(names))


def passed(oid, merge=False):
    return CommitCi(oid, merge, "SUCCESS", ())


# ---- reading GitHub's answer ----------------------------------------------------------


def test_a_commits_failing_checks_are_the_newest_runs_failures():
    commits = parse_history(
        history(
            node(
                "a",
                "FAILURE",
                [
                    check_run(
                        "Integration tests status",
                        "FAILURE",
                        run=1,
                        at="2026-10-22T01:00:00Z",
                    ),
                    check_run(
                        "Integration tests status",
                        "SUCCESS",
                        run=2,
                        at="2026-10-22T02:00:00Z",
                    ),
                    check_run("build", "SUCCESS", run=2, at="2026-10-22T02:00:00Z"),
                ],
            ),
            node(
                "b",
                "FAILURE",
                [check_run("lint", "FAILURE"), check_run("build", "SUCCESS")],
            ),
        )
    )

    assert [(c.oid, c.rollup, c.failing) for c in commits] == [
        ("a", "FAILURE", ()),  # failed only in a run a rerun replaced
        ("b", "FAILURE", ("lint",)),
    ]


def test_merge_commits_and_commits_without_ci_are_read():
    commits = parse_history(history(node("a", "SUCCESS", parents=2), node("b", None)))

    assert [(c.oid, c.merge, c.rollup) for c in commits] == [
        ("a", True, "SUCCESS"),
        ("b", False, None),
    ]


def test_a_status_context_is_named_by_its_context():
    commits = parse_history(
        history(
            node(
                "a",
                "FAILURE",
                [
                    {
                        "__typename": "StatusContext",
                        "context": "ci/legacy",
                        "state": "FAILURE",
                    }
                ],
            )
        )
    )

    assert commits[0].failing == ("ci/legacy",)


# ---- the sentence ---------------------------------------------------------------------


def test_no_failed_commit_means_nothing_to_explain():
    assert explain([passed("a"), passed("b")]) is None
    assert explain([]) is None


def test_failures_that_come_and_go_are_called_flaky_and_the_checks_are_named():
    commits = [
        failed("a", "Integration tests status"),
        passed("b"),
        failed("c", "Integration tests status", "Claim warm env"),
        passed("d"),
        passed("e"),
    ]

    text = explain(commits)

    assert text is not None
    assert text.startswith(
        "CI failed on 2 of the last 5 commits, most often in "
        "Integration tests status (2), Claim warm env (1)."
    )
    assert "CI passed on 3 other commits, so the failures come and go" in text
    assert "flaky or infrastructure" in text


def test_the_same_check_failing_on_the_newest_commits_is_a_persistent_failure():
    commits = [
        passed("a"),
        failed("b", "lint"),
        failed("c", "lint", "build"),
        failed("d", "lint"),
    ]

    text = explain(commits)

    assert text is not None
    assert "The last 3 commits all fail in lint: a failure that persists" in text
    assert "come and go" not in text


def test_merges_of_other_branches_are_counted():
    commits = [failed("a", "x", merge=True), passed("b", merge=True), passed("c")]

    text = explain(commits)

    assert (
        text is not None
        and "2 of the last 3 commits are merges of other branches" in text
    )


def test_a_failure_in_a_replaced_run_only_is_counted_but_names_no_check():
    text = explain([CommitCi("a", False, "FAILURE", ()), passed("b")])

    assert text is not None
    assert text.startswith(
        "CI failed on 1 of the last 2 commits."
    )  # no ", most often in"


def test_commits_without_ci_are_not_counted_among_those_judged():
    text = explain([CommitCi("a", False, None, ()), failed("b", "x"), passed("c")])

    assert text is not None and text.startswith("CI failed on 1 of the last 2 commits")


# ---- looking it up --------------------------------------------------------------------


def test_the_explainer_asks_for_the_prs_commits_of_the_repos_slug():
    asked = []

    async def graphql(query, variables):
        asked.append((query, dict(variables)))
        return history(
            node("a", "FAILURE", [check_run("lint", "FAILURE")]),
            node("b", "FAILURE", [check_run("lint", "FAILURE")]),
        )

    async def slug_of(path):
        return "dbt-labs/ai-codegen-api"

    explainer = ci_explainer(graphql, slug_of)

    text = asyncio.run(explainer(__import__("pathlib").Path("/r"), 1804))

    assert asked == [
        (
            CI_HISTORY_QUERY,
            {"owner": "dbt-labs", "repo": "ai-codegen-api", "number": 1804},
        )
    ]
    assert text is not None and "lint" in text


def test_a_repo_with_no_github_remote_has_nothing_to_look_up():
    async def graphql(query, variables):
        raise AssertionError("must not be asked")

    async def slug_of(path):
        return None

    assert (
        asyncio.run(ci_explainer(graphql, slug_of)(__import__("pathlib").Path("/r"), 1))
        is None
    )


def test_the_query_asks_for_what_the_explanation_reads():
    for needed in (
        "parents",
        "workflowRun",
        "databaseId",
        "createdAt",
        "conclusion",
        "context",
    ):
        assert needed in CI_HISTORY_QUERY
