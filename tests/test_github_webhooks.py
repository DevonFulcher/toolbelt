"""Which branches and PRs a GitHub webhook payload names."""

from toolbelt.ui.github_webhooks import Affected, affected

REPO = {"full_name": "DBT-Labs/Api"}


def hit(branches=(), numbers=()):
    return Affected("dbt-labs/api", frozenset(branches), frozenset(numbers))


def test_pull_request_events_name_the_pr_and_its_head_branch():
    payload = {
        "repository": REPO,
        "pull_request": {"number": 7, "head": {"ref": "devon/a"}},
    }

    for event in (
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_review_thread",
    ):
        assert affected(event, payload) == hit(["devon/a"], [7])


def test_a_check_run_names_its_pull_requests_and_head_branch():
    payload = {
        "repository": REPO,
        "check_run": {
            "pull_requests": [{"number": 7, "head": {"ref": "devon/a"}}],
            "check_suite": {"head_branch": "devon/a"},
        },
    }

    assert affected("check_run", payload) == hit(["devon/a"], [7])


def test_a_check_suite_without_pull_requests_still_names_its_branch():
    payload = {
        "repository": REPO,
        "check_suite": {"pull_requests": [], "head_branch": "devon/b"},
    }

    assert affected("check_suite", payload) == hit(["devon/b"])


def test_a_status_names_the_branches_at_its_commit():
    payload = {
        "repository": REPO,
        "branches": [{"name": "devon/a"}, {"name": "main"}],
    }

    assert affected("status", payload) == hit(["devon/a", "main"])


def test_a_comment_on_a_pull_request_names_its_number():
    payload = {
        "repository": REPO,
        "issue": {"number": 7, "pull_request": {"url": "u"}},
    }

    assert affected("issue_comment", payload) == hit(numbers=[7])


def test_a_comment_on_a_plain_issue_names_nothing():
    payload = {"repository": REPO, "issue": {"number": 7}}

    assert affected("issue_comment", payload) is None


def test_a_push_names_the_branch_but_not_a_tag():
    assert affected("push", {"repository": REPO, "ref": "refs/heads/devon/a"}) == hit(
        ["devon/a"]
    )
    assert affected("push", {"repository": REPO, "ref": "refs/tags/v1"}) is None


def test_a_ping_names_nothing():
    assert affected("ping", {"zen": "Keep it logically awesome."}) is None
    assert affected("ping", {"repository": REPO}) is None
