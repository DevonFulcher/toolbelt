"""Unit tests for the ASCII stack rendering (pure function)."""

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.viz import render


def test_single_chain():
    parents = {
        "devon/api": "main",
        "devon/api_tests": "devon/api",
    }
    assert render(parents) == ("main\n└─ devon/api\n   └─ devon/api_tests")


def test_branching_tree():
    parents = {
        "devon/api": "main",
        "devon/api_docs": "devon/api",
        "devon/api_tests": "devon/api",
    }
    assert render(parents) == (
        "main\n" "└─ devon/api\n" "   ├─ devon/api_docs\n" "   └─ devon/api_tests"
    )


def test_current_branch_marker():
    parents = {"devon/api": "main"}
    assert render(parents, current="devon/api") == "main\n└─ devon/api *"
    # The base can be marked too.
    assert render(parents, current="main") == "main *\n└─ devon/api"


def test_empty():
    assert render({}) == ""


def test_no_statuses_renders_plain_tree():
    parents = {"devon/api": "main"}
    # Omitting `statuses` (the default) must match old behavior exactly.
    assert render(parents) == render(parents, statuses=None)


def test_pending_branch_shows_loading_placeholder():
    parents = {"devon/api": "main"}
    assert render(parents, statuses={}) == "main\n└─ devon/api  ⏳ loading"


def test_resolved_status_is_appended_and_root_is_untouched():
    parents = {"devon/api": "main"}
    statuses = {
        "devon/api": BranchStatus(
            pr=PrState.OPEN, ci=CiState.SUCCESS, review=ReviewState.APPROVED, url=None
        )
    }
    assert render(parents, statuses=statuses) == (
        "main\n└─ devon/api  PR open    CI success  review approved"
    )


def test_status_column_aligns_across_varying_depths():
    parents = {
        "devon/api": "main",
        "devon/api_tests": "devon/api",
    }
    statuses = {
        "devon/api": BranchStatus(
            pr=PrState.DRAFT, ci=CiState.NONE, review=ReviewState.NONE, url=None
        ),
        "devon/api_tests": BranchStatus(
            pr=PrState.NONE, ci=CiState.NONE, review=ReviewState.NONE, url=None
        ),
    }
    rendered = render(parents, statuses=statuses)
    lines = rendered.splitlines()
    # The status suffix starts at the same column on every row.
    assert lines[1].index("PR draft") == lines[2].index("PR none")


def test_pr_url_is_appended_after_the_status_when_present():
    parents = {"devon/api": "main"}
    statuses = {
        "devon/api": BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.SUCCESS,
            review=ReviewState.APPROVED,
            url="https://github.com/acme/widgets/pull/1",
        )
    }
    assert render(parents, statuses=statuses) == (
        "main\n└─ devon/api  PR open    CI success  review approved"
        "  https://github.com/acme/widgets/pull/1"
    )


def test_no_pr_url_column_when_branch_has_no_pr_or_is_still_loading():
    parents = {
        "devon/api": "main",
        "devon/api_tests": "devon/api",
    }
    statuses = {
        "devon/api": BranchStatus(
            pr=PrState.NONE, ci=CiState.NONE, review=ReviewState.NONE, url=None
        )
        # "devon/api_tests" omitted: still loading.
    }
    rendered = render(parents, statuses=statuses)
    assert "http" not in rendered


def test_pr_url_column_aligns_despite_differing_status_suffix_length():
    parents = {
        "devon/api": "main",
        "devon/api_tests": "devon/api",
    }
    statuses = {
        "devon/api": BranchStatus(
            pr=PrState.DRAFT,
            ci=CiState.NONE,
            review=ReviewState.NONE,
            url="https://github.com/acme/widgets/pull/1",
        ),
        "devon/api_tests": BranchStatus(
            pr=PrState.OPEN,
            ci=CiState.SUCCESS,
            review=ReviewState.APPROVED,
            url="https://github.com/acme/widgets/pull/2",
        ),
    }
    rendered = render(parents, statuses=statuses)
    lines = rendered.splitlines()
    # "PR draft" (shorter suffix) and "PR open  CI success  review approved"
    # (longer suffix) must still start their URL at the same column.
    assert lines[1].index("https://") == lines[2].index("https://")
