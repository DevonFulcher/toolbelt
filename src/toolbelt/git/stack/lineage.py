"""Stack lineage: which branch is stacked on which.

Parent pointers are persisted in git config under the ``toolbelt-stack``
section, one key per tracked branch::

    toolbelt-stack.<branch>.parent = <parent-branch>

This is the entire data model. The stack tree is reconstructed by reading every
``toolbelt-stack.*.parent`` key. Functions take ``root`` (the repo/worktree
path) explicitly so they are easy to test against a throwaway repo.
"""

import re
from collections import defaultdict
from pathlib import Path

from toolbelt.git.exec import run

SECTION = "toolbelt-stack"
_KEY_SUFFIX = ".parent"

# parents maps a branch -> its parent branch.
Parents = dict[str, str]
# children maps a branch -> its sorted child branches.
Children = dict[str, list[str]]


def _parent_key(branch: str) -> str:
    return f"{SECTION}.{branch}{_KEY_SUFFIX}"


def get_parent(branch: str, *, root: Path) -> str | None:
    """Return ``branch``'s recorded parent, or ``None`` if it is not tracked."""
    result = run(
        ["git", "config", "--get", _parent_key(branch)],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def set_parent(branch: str, parent: str, *, root: Path) -> None:
    """Record ``parent`` as ``branch``'s parent."""
    run(["git", "config", _parent_key(branch), parent], cwd=root)


def remove_parent(branch: str, *, root: Path) -> None:
    """Drop ``branch`` from the stack (no-op if it was untracked)."""
    run(
        ["git", "config", "--unset", _parent_key(branch)],
        cwd=root,
        check=False,
    )


def all_parents(*, root: Path) -> Parents:
    """Read every recorded ``branch -> parent`` mapping for this repo."""
    result = run(
        [
            "git",
            "config",
            "--get-regexp",
            rf"^{re.escape(SECTION)}\..*{re.escape(_KEY_SUFFIX)}$",
        ],
        cwd=root,
        check=False,
        capture_output=True,
    )
    parents: Parents = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        # Strip the leading "toolbelt-stack." and trailing ".parent"; the
        # remainder is the branch name (which may itself contain dots/slashes).
        branch = key[len(SECTION) + 1 : -len(_KEY_SUFFIX)]
        value = value.strip()
        if branch and value:
            parents[branch] = value
    return parents


def children_map(parents: Parents) -> Children:
    """Invert ``parents`` into parent -> sorted children."""
    children: dict[str, list[str]] = defaultdict(list)
    for child, parent in parents.items():
        children[parent].append(child)
    for kids in children.values():
        kids.sort()
    return dict(children)


def roots(parents: Parents) -> list[str]:
    """Return base branches: referenced as a parent but not themselves tracked.

    For a stack rooted at ``main`` this is ``["main"]`` — ``main`` is a parent
    value but has no ``toolbelt-stack`` entry of its own.
    """
    tracked = set(parents.keys())
    referenced = set(parents.values())
    return sorted(referenced - tracked)


def stack_root(branch: str, *, root: Path) -> str:
    """Return the top-of-stack branch for ``branch`` (the child of the base).

    Walks parent pointers upward until the next parent up is untracked (the
    base, e.g. ``main``). If ``branch`` itself is untracked, it is returned.
    """
    node = branch
    while True:
        parent = get_parent(node, root=root)
        if parent is None:
            return node
        if get_parent(parent, root=root) is None:
            # parent is the base; node is the top of the stack.
            return node
        node = parent


def subtree_topo(start: str, parents: Parents) -> list[str]:
    """Branches in ``start``'s subtree, ordered root -> leaf (parents first)."""
    children = children_map(parents)
    order: list[str] = []

    def visit(node: str) -> None:
        order.append(node)
        for child in children.get(node, []):
            visit(child)

    visit(start)
    return order


def resolve_stack(branch: str, *, root: Path) -> list[str]:
    """All branches in ``branch``'s stack, ordered root -> leaf.

    The stack is the whole subtree hanging off the top-of-stack branch, so a
    sync touches every sibling/descendant, not just ``branch``'s direct line.
    """
    top = stack_root(branch, root=root)
    return subtree_topo(top, all_parents(root=root))
