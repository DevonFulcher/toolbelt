"""Unit tests for the ASCII stack rendering (pure function)."""

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
