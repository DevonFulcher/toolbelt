"""Tests for stack lineage.

The config read/write paths run real git against the ``repo`` fixture; the tree
helpers are pure and tested directly.
"""

from pathlib import Path

from toolbelt.git.stack import lineage


def test_get_parent_untracked_returns_none(repo: Path):
    assert lineage.get_parent("devon/api", root=repo) is None


def test_set_get_remove_parent(repo: Path):
    lineage.set_parent("devon/api", "main", root=repo)
    assert lineage.get_parent("devon/api", root=repo) == "main"
    lineage.remove_parent("devon/api", root=repo)
    assert lineage.get_parent("devon/api", root=repo) is None


def test_branch_name_with_slashes_and_dots(repo: Path):
    lineage.set_parent("devon/feature.v2", "main", root=repo)
    assert lineage.get_parent("devon/feature.v2", root=repo) == "main"
    assert lineage.all_parents(root=repo) == {"devon/feature.v2": "main"}


def test_all_parents(repo: Path):
    lineage.set_parent("devon/api", "main", root=repo)
    lineage.set_parent("devon/api_tests", "devon/api", root=repo)
    assert lineage.all_parents(root=repo) == {
        "devon/api": "main",
        "devon/api_tests": "devon/api",
    }


def test_children_map():
    parents = {
        "devon/api": "main",
        "devon/api_docs": "devon/api",
        "devon/api_tests": "devon/api",
    }
    assert lineage.children_map(parents) == {
        "main": ["devon/api"],
        # children are sorted for deterministic ordering
        "devon/api": ["devon/api_docs", "devon/api_tests"],
    }


def test_roots():
    parents = {"devon/api": "main", "devon/api_tests": "devon/api"}
    assert lineage.roots(parents) == ["main"]


def test_subtree_topo_is_root_to_leaf():
    parents = {
        "devon/api": "main",
        "devon/api_docs": "devon/api",
        "devon/api_tests": "devon/api",
    }
    order = lineage.subtree_topo("devon/api", parents)
    # parent always precedes its children
    assert order[0] == "devon/api"
    assert order.index("devon/api") < order.index("devon/api_docs")
    assert order.index("devon/api") < order.index("devon/api_tests")
    assert set(order) == {"devon/api", "devon/api_docs", "devon/api_tests"}


def test_stack_root_and_resolve_stack(repo: Path):
    # main <- api <- api_tests, and a sibling api_docs off api
    lineage.set_parent("devon/api", "main", root=repo)
    lineage.set_parent("devon/api_tests", "devon/api", root=repo)
    lineage.set_parent("devon/api_docs", "devon/api", root=repo)

    # top-of-stack for any member is the child of the base (devon/api)
    assert lineage.stack_root("devon/api_tests", root=repo) == "devon/api"
    assert lineage.stack_root("devon/api", root=repo) == "devon/api"

    # resolving from a leaf returns the whole stack, root -> leaf
    stack = lineage.resolve_stack("devon/api_tests", root=repo)
    assert stack[0] == "devon/api"
    assert set(stack) == {"devon/api", "devon/api_tests", "devon/api_docs"}
