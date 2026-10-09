"""Unit tests for parsing/formatting `gh pr view` payloads (pure functions)."""

from toolbelt.git.stack.status import (
    NO_PR,
    BranchStatus,
    CiState,
    PrState,
    ReviewState,
    _ci_state,
    _count_unresolved,
    _parse_status,
    format_status,
)


def test_no_pr_has_no_ci_or_review():
    assert NO_PR == BranchStatus(
        pr=PrState.NONE, ci=CiState.NONE, review=ReviewState.NONE, url=None
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
        "url": "https://github.com/acme/widgets/pull/1",
    }
    assert _parse_status(data) == BranchStatus(
        pr=PrState.DRAFT,
        ci=CiState.NONE,
        review=ReviewState.NONE,
        url="https://github.com/acme/widgets/pull/1",
    )


def test_parse_status_open_pr_with_pending_review_request():
    data = {
        "state": "OPEN",
        "isDraft": False,
        "reviewDecision": "",
        "reviewRequests": [{"login": "someone"}],
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        "url": "https://github.com/acme/widgets/pull/2",
    }
    assert _parse_status(data) == BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.SUCCESS,
        review=ReviewState.REQUESTED,
        url="https://github.com/acme/widgets/pull/2",
    )


def test_parse_status_changes_requested_maps_to_reviewed():
    data = {
        "state": "OPEN",
        "isDraft": False,
        "reviewDecision": "CHANGES_REQUESTED",
        "reviewRequests": [],
        "statusCheckRollup": [],
        "url": "https://github.com/acme/widgets/pull/3",
    }
    assert _parse_status(data).review == ReviewState.REVIEWED


def test_parse_status_merged_pr():
    data = {
        "state": "MERGED",
        "isDraft": False,
        "reviewDecision": "APPROVED",
        "reviewRequests": [],
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        "url": "https://github.com/acme/widgets/pull/4",
    }
    assert _parse_status(data) == BranchStatus(
        pr=PrState.MERGED,
        ci=CiState.SUCCESS,
        review=ReviewState.APPROVED,
        url="https://github.com/acme/widgets/pull/4",
    )


def test_format_status_omits_ci_and_review_when_none():
    status = BranchStatus(
        pr=PrState.DRAFT, ci=CiState.NONE, review=ReviewState.NONE, url=None
    )
    assert format_status(status) == "PR draft"


def test_format_status_aligns_ci_column_despite_differing_pr_word_length():
    # "open" (4 chars) and "merged" (6 chars) must not shift where "CI" starts.
    open_pr = format_status(
        BranchStatus(
            pr=PrState.OPEN, ci=CiState.FAILED, review=ReviewState.NONE, url=None
        )
    )
    merged_pr = format_status(
        BranchStatus(
            pr=PrState.MERGED, ci=CiState.SUCCESS, review=ReviewState.NONE, url=None
        )
    )
    assert open_pr.index("CI") == merged_pr.index("CI")


def test_format_status_aligns_review_column_despite_differing_ci_word_length():
    # "failed" (6 chars) and "success" (7 chars) must not shift "review".
    failed_ci = format_status(
        BranchStatus(
            pr=PrState.OPEN, ci=CiState.FAILED, review=ReviewState.APPROVED, url=None
        )
    )
    success_ci = format_status(
        BranchStatus(
            pr=PrState.OPEN, ci=CiState.SUCCESS, review=ReviewState.APPROVED, url=None
        )
    )
    assert failed_ci.index("review") == success_ci.index("review")


def test_count_unresolved_counts_only_unresolved_threads():
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {"isResolved": False},
                            {"isResolved": True},
                            {"isResolved": False},
                        ]
                    }
                }
            }
        }
    }
    assert _count_unresolved(payload) == 2


def test_count_unresolved_is_zero_when_no_threads():
    payload = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}
    }
    assert _count_unresolved(payload) == 0


def test_format_status_shows_open_comments_only_when_nonzero():
    no_comments = BranchStatus(
        pr=PrState.OPEN, ci=CiState.NONE, review=ReviewState.NONE, url=None
    )
    assert "open comments" not in format_status(no_comments)

    with_comments = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.NONE,
        review=ReviewState.NONE,
        url=None,
        open_comments=3,
    )
    assert format_status(with_comments).endswith("open comments: 3")


def test_parse_status_carries_through_open_comments():
    data = {
        "state": "OPEN",
        "isDraft": False,
        "reviewDecision": "",
        "reviewRequests": [],
        "statusCheckRollup": [],
        "url": "https://github.com/acme/widgets/pull/5",
    }
    assert _parse_status(data, open_comments=4).open_comments == 4


def _open_pr_payload(mergeable: str, state: str = "OPEN") -> dict:
    return {
        "state": state,
        "isDraft": False,
        "reviewDecision": "",
        "reviewRequests": [],
        "statusCheckRollup": [],
        "url": "https://github.com/acme/widgets/pull/6",
        "mergeable": mergeable,
    }


def test_parse_status_flags_conflicting_open_pr():
    assert _parse_status(_open_pr_payload("CONFLICTING")).has_conflicts


def test_parse_status_does_not_flag_mergeable_or_unknown():
    assert not _parse_status(_open_pr_payload("MERGEABLE")).has_conflicts
    assert not _parse_status(_open_pr_payload("UNKNOWN")).has_conflicts


def test_parse_status_ignores_conflicts_on_a_merged_pr():
    assert not _parse_status(_open_pr_payload("CONFLICTING", "MERGED")).has_conflicts


def test_format_status_shows_merge_conflicts_only_when_present():
    clean = BranchStatus(
        pr=PrState.OPEN, ci=CiState.NONE, review=ReviewState.NONE, url=None
    )
    assert "merge conflicts" not in format_status(clean)

    conflicted = BranchStatus(
        pr=PrState.OPEN,
        ci=CiState.NONE,
        review=ReviewState.NONE,
        url=None,
        has_conflicts=True,
    )
    assert format_status(conflicted).endswith("merge conflicts")


def test_format_status_columns_are_stable_regardless_of_which_are_present():
    sparse = format_status(
        BranchStatus(
            pr=PrState.DRAFT,
            ci=CiState.SUCCESS,
            review=ReviewState.NONE,
            url=None,
            has_conflicts=True,
        )
    )
    full = format_status(
        BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.SUCCESS,
            review=ReviewState.REQUESTED,
            url=None,
            open_comments=2,
            has_conflicts=True,
        )
    )
    assert sparse.index("merge conflicts") == full.index("merge conflicts")
    assert sparse.index("CI") == full.index("CI")
