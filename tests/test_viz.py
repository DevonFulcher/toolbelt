"""Unit tests for the ASCII stack rendering (pure function)."""

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.viz import Columns, render


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
        "main\n└─ devon/api\n   ├─ devon/api_docs\n   └─ devon/api_tests"
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


def _status(**changes) -> BranchStatus:
    base = {
        "pr": PrState.OPEN,
        "ci": CiState.SUCCESS,
        "review": ReviewState.NONE,
        "url": None,
    }
    return BranchStatus(**{**base, **changes})


def test_no_columns_is_the_plain_status_rendering():
    parents = {"devon/api": "main"}
    statuses = {"devon/api": _status(additions=5, deletions=1, assignees=("me",))}

    assert render(parents, statuses=statuses, columns=Columns()) == render(
        parents, statuses=statuses
    )


def test_lines_and_assignee_columns_follow_the_status_and_line_up():
    parents = {"devon/api": "main", "devon/api_tests": "devon/api"}
    statuses = {
        "devon/api": _status(additions=100, deletions=26, assignees=("me", "you")),
        "devon/api_tests": _status(
            review=ReviewState.REQUESTED,
            additions=7,
            deletions=0,
            reviewers=("danielbdias",),
        ),
    }

    rendered = render(
        parents, statuses=statuses, columns=Columns(lines=True, assignee=True)
    ).splitlines()

    assert rendered[1].endswith("+100/-26  assigned: me, you")
    assert rendered[2].endswith("+7/-0     review requested: danielbdias")
    assert rendered[1].index("+100") == rendered[2].index("+7/-0")


def test_a_pr_that_is_done_shows_no_lines_or_assignee():
    parents = {"devon/api": "main"}
    statuses = {
        "devon/api": _status(
            pr=PrState.MERGED, additions=5, deletions=1, assignees=("me",)
        )
    }

    rendered = render(
        parents, statuses=statuses, columns=Columns(lines=True, assignee=True)
    )

    assert "+5/-1" not in rendered and "assigned" not in rendered


def test_next_column_uses_the_same_rules_as_the_ui():
    parents = {"devon/api": "main", "devon/api_tests": "devon/api"}
    statuses = {
        "devon/api": _status(pr=PrState.DRAFT),
        "devon/api_tests": _status(review=ReviewState.APPROVED),
    }

    rendered = render(parents, statuses=statuses, columns=Columns(next=True))

    assert "Publish PR in GitHub" in rendered.splitlines()[1]
    # Approved, but its parent has not merged: blocked on it, not "Merge".
    assert "Blocked on devon/api" in rendered.splitlines()[2]


def test_columns_wait_for_a_branch_whose_status_is_loading():
    parents = {"devon/api": "main", "devon/api_tests": "devon/api"}
    statuses = {"devon/api": _status(additions=1, deletions=2)}

    rendered = render(parents, statuses=statuses, columns=Columns(lines=True))

    assert rendered.splitlines()[1].endswith("+1/-2")
    assert rendered.splitlines()[2].endswith("⏳ loading")
