"""Stack lineage: which branch is stacked on which.

Parent pointers are persisted in toolbelt's SQLite stack DB (see ``store``),
keyed by repo. The first time a repo is seen, any legacy
``toolbelt-stack.<branch>.parent`` git config keys are imported (the git config
itself is left untouched and no longer written). Functions take ``root`` (the
repo/worktree path) explicitly so they are easy to test against a throwaway
repo.
"""

import contextlib
import functools
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from toolbelt.git.exec import capture, run
from toolbelt.git.stack.store import (
    Parents,
    RepoIdentity,
    RepoRecord,
    StackStore,
    default_db_path,
)

SECTION = "toolbelt-stack"
_KEY_SUFFIX = ".parent"

# children maps a branch -> its sorted child branches.
Children = dict[str, list[str]]


def _legacy_parents(*, root: Path) -> Parents:
    """Read the pre-SQLite ``toolbelt-stack.*.parent`` git config keys."""
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


@functools.cache
def repo_identity(root: Path) -> RepoIdentity:
    """Identify the repo ``root`` (any of its worktrees) belongs to."""
    common = Path(capture(["git", "rev-parse", "--git-common-dir"], cwd=root))
    if not common.is_absolute():
        common = root / common
    common = common.resolve()
    remote = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return RepoIdentity(
        git_common_dir=common,
        path=common.parent if common.name == ".git" else common,
        remote_url=(remote.stdout.strip() or None) if remote.returncode == 0 else None,
    )


@contextlib.contextmanager
def _repo_store(root: Path) -> Iterator[tuple[StackStore, int]]:
    """Open the stack DB and the repo's id, importing legacy git config the
    first time this repo is seen."""
    store = StackStore(default_db_path())
    try:
        repo_id = store.ensure_repo(
            repo_identity(root), lambda: _legacy_parents(root=root)
        )
        yield store, repo_id
    finally:
        store.close()


def get_parent(branch: str, *, root: Path) -> str | None:
    """Return ``branch``'s recorded parent, or ``None`` if it is not tracked."""
    with _repo_store(root) as (store, repo_id):
        return store.get_parent(repo_id, branch)


def set_parent(branch: str, parent: str, *, root: Path) -> None:
    """Record ``parent`` as ``branch``'s parent."""
    with _repo_store(root) as (store, repo_id):
        store.set_parent(repo_id, branch, parent)


def remove_parent(branch: str, *, root: Path) -> None:
    """Drop ``branch`` from the stack (no-op if it was untracked)."""
    with _repo_store(root) as (store, repo_id):
        store.remove_branch(repo_id, branch)


def all_parents(*, root: Path) -> Parents:
    """Read every recorded ``branch -> parent`` mapping for this repo."""
    with _repo_store(root) as (store, repo_id):
        return store.all_parents(repo_id)


def known_repos() -> list[tuple[RepoRecord, Parents]]:
    """Every repo in the stack DB with its ``branch -> parent`` mapping."""
    store = StackStore(default_db_path())
    try:
        return [(repo, store.all_parents(repo.id)) for repo in store.repos()]
    finally:
        store.close()


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
