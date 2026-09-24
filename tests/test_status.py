"""Unit tests for parsing/formatting `gh pr view` payloads (pure functions)."""

from toolbelt.git.stack.status import (
    NO_PR,
    BranchStatus,
    CiState,
    PrState,
    ReviewState,
    _ci_state,
    _parse_status,
    format_status,
)


def test_no_pr_has_no_ci_or_review():
    assert NO_PR == BranchStatus(
        pr=PrState.NONE, ci=CiState.NONE, review=ReviewState.NONE
    )
    assert format_status(NO_PR) == "PR none"


def test_ci_state_prioritizes_failure_over_running():
    rollup = [
        {"conclusion": "SUCCESS"},
        {"conclusion": "FAILURE"},
        {"status": "IN_PROGRESS"},
    ]
    assert _ci_state(rollup) == CiState.FAILED


def test_ci_state_running_when_nothing_failed_yet():
    rollup = [{"conclusion": "SUCCESS"}, {"status": "QUEUED"}]
    assert _ci_state(rollup) == CiState.RUNNING


def test_ci_state_success_ignores_skipped_and_neutral():
    rollup = [
        {"conclusion": "SUCCESS"},
        {"conclusion": "SKIPPED"},
        {"conclusion": "NEUTRAL"},
    ]
    assert _ci_state(rollup) == CiState.SUCCESS


def test_ci_state_none_when_no_checks_configured():
    assert _ci_state([]) == CiState.NONE


def test_parse_status_draft_pr():
    data = {
        "state": "OPEN",
        "isDraft": True,
        "reviewDecision": "",
        "reviewRequests": [],
        "statusCheckRollup": [],
    }
    assert _parse_status(data) == BranchStatus(
        pr=PrState.DRAFT, ci=CiState.NONE, review=ReviewState.NONE
    )


def test_parse_status_open_pr_with_pending_review_request():
    data = {
        "state": "OPEN",
        "isDraft": False,
        "reviewDecision": "",
        "reviewRequests": [{"login": "someone"}],
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }
    assert _parse_status(data) == BranchStatus(
        pr=PrState.OPEN, ci=CiState.SUCCESS, review=ReviewState.REQUESTED
    )


def test_parse_status_changes_requested_maps_to_reviewed():
    data = {
        "state": "OPEN",
        "isDraft": False,
        "reviewDecision": "CHANGES_REQUESTED",
        "reviewRequests": [],
        "statusCheckRollup": [],
    }
    assert _parse_status(data).review == ReviewState.REVIEWED


def test_parse_status_merged_pr():
    data = {
        "state": "MERGED",
        "isDraft": False,
        "reviewDecision": "APPROVED",
        "reviewRequests": [],
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }
    assert _parse_status(data) == BranchStatus(
        pr=PrState.MERGED, ci=CiState.SUCCESS, review=ReviewState.APPROVED
    )


def test_format_status_omits_ci_and_review_when_none():
    status = BranchStatus(pr=PrState.DRAFT, ci=CiState.NONE, review=ReviewState.NONE)
    assert format_status(status) == "PR draft"


def test_format_status_aligns_ci_column_despite_differing_pr_word_length():
    # "open" (4 chars) and "merged" (6 chars) must not shift where "CI" starts.
    open_pr = format_status(
        BranchStatus(pr=PrState.OPEN, ci=CiState.FAILED, review=ReviewState.NONE)
    )
    merged_pr = format_status(
        BranchStatus(pr=PrState.MERGED, ci=CiState.SUCCESS, review=ReviewState.NONE)
    )
    assert open_pr.index("CI") == merged_pr.index("CI")


def test_format_status_aligns_review_column_despite_differing_ci_word_length():
    # "failed" (6 chars) and "success" (7 chars) must not shift "review".
    failed_ci = format_status(
        BranchStatus(pr=PrState.OPEN, ci=CiState.FAILED, review=ReviewState.APPROVED)
    )
    success_ci = format_status(
        BranchStatus(pr=PrState.OPEN, ci=CiState.SUCCESS, review=ReviewState.APPROVED)
    )
    assert failed_ci.index("review") == success_ci.index("review")
