"""Which pairs of branch heads merge cleanly, tried with ``git merge-tree``.

A pair of commits is merged in memory (``git merge-tree --write-tree``, which
touches no checkout) and its conflicted files are listed. The answer is a pure
function of the two commits, their natural merge base included, so it is kept by
(repo, oid, oid) in the ``sibling_conflicts`` table of the stack DB. ``tt ui``
reads it to tell a card which other open tasks its branch would conflict with
(see ``toolbelt.ui.sibling_conflicts``), and ``tt g save`` records the pairs of
the heads it moved so the answer is there before a browser is open.
"""

import re
import sqlite3
from pathlib import Path
from typing import Protocol

from toolbelt import logged_process
from toolbelt.git.stack.lineage import repo_identity
from toolbelt.git.stack.store import Parents, StackStore, default_db_path
from toolbelt.logger import logger

type Pair = tuple[str, str]

_OID_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


class ConflictCache(Protocol):
    """Where the pairs already tried are kept (the ``sibling_conflicts``
    table). Called from worker threads."""

    def load(self, repo_id: int) -> dict[Pair, tuple[str, ...]]: ...

    def save(self, repo_id: int, pairs: dict[Pair, tuple[str, ...]]) -> None: ...

    def prune(self, repo_id: int, heads: set[str]) -> None: ...


def _git(root: Path, *args: str, stdin: str | None = None):
    return logged_process.run(
        ["git", *args],
        cwd=root,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def sorted_pair(a: str, b: str) -> Pair:
    return (a, b) if a <= b else (b, a)


def merge_conflicts(root: Path, pairs: list[Pair]) -> dict[Pair, tuple[str, ...]]:
    """The conflicted files of merging each pair of commits (empty when it
    merges cleanly), tried by one ``git merge-tree --stdin``. Every oid must be
    a commit in the repo: git stops the whole batch on one that is not, and
    that is raised, never read as a conflict.

    With ``--stdin`` git writes, for each line and NUL separated, ``1`` (clean)
    or ``0`` (conflicts), the merged tree and, with ``--name-only``, the
    conflicted files followed by an empty field."""
    if not pairs:
        return {}
    result = _git(
        root,
        "merge-tree",
        "--stdin",
        "--write-tree",
        "--name-only",
        "--no-messages",
        stdin="".join(f"{a} {b}\n" for a, b in pairs),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git merge-tree failed")
    fields = result.stdout.split("\0")
    found: dict[Pair, tuple[str, ...]] = {}
    at = 0
    for pair in pairs:
        status, tree = fields[at], fields[at + 1]
        if status not in ("0", "1") or not _OID_RE.fullmatch(tree):
            raise RuntimeError(f"unexpected git merge-tree output for {pair}")
        at += 2
        files = []
        while fields[at]:
            files.append(fields[at])
            at += 1
        at += 1
        if (status == "0") != bool(files):
            raise RuntimeError(f"unexpected git merge-tree output for {pair}")
        found[pair] = tuple(files)
    return found


def local_heads(root: Path) -> dict[str, str]:
    """Every local branch's commit."""
    result = _git(
        root, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads"
    )
    heads = {}
    for line in result.stdout.splitlines():
        oid, _, ref = line.partition(" ")
        heads[ref.removeprefix("refs/heads/")] = oid
    return heads


def existing_commits(root: Path, oids: set[str]) -> set[str]:
    """The ``oids`` that are commits in the repo."""
    if not oids:
        return set()
    result = _git(
        root, "cat-file", "--batch-check", stdin="".join(f"{o}\n" for o in oids)
    )
    present = set()
    for line in result.stdout.splitlines():
        oid, _, rest = line.partition(" ")
        if rest.startswith("commit "):
            present.add(oid)
    return present


def _ancestors(parents: Parents, branch: str) -> set[str]:
    found: set[str] = set()
    parent = parents.get(branch)
    while parent is not None and parent not in found:
        found.add(parent)
        parent = parents.get(parent)
    return found


def pairs_to_try(parents: Parents, branches: set[str]) -> list[tuple[str, str]]:
    """Every pair of ``branches`` that are not in one chain of the stack."""
    ordered = sorted(branches)
    return [
        (a, b)
        for index, a in enumerate(ordered)
        for b in ordered[index + 1 :]
        if a not in _ancestors(parents, b) and b not in _ancestors(parents, a)
    ]


def untried_pairs(
    parents: Parents,
    heads: dict[str, str | None],
    tried: dict[Pair, tuple[str, ...]],
) -> list[Pair]:
    """The pairs of different commits among ``heads`` (``None``: head not
    local) that are not in one chain of the stack and not yet in ``tried``."""
    return sorted(
        {
            sorted_pair(oid_a, oid_b)
            for a, b in pairs_to_try(parents, set(heads))
            if (oid_a := heads[a]) and (oid_b := heads[b]) and oid_a != oid_b
        }
        - set(tried)
    )


def record_sibling_conflicts(root: Path) -> None:
    """Tries the pairs of tracked branch heads (local refs) of the repo of
    ``root`` that the ``sibling_conflicts`` table does not have, and stores them.

    Only records: nothing is shown or asked, and a failure is logged and
    dropped, because a save must not stop on it (``tt ui`` tries any pair that is
    still missing).
    """
    try:
        store = StackStore(default_db_path())
        try:
            repo_id = store.ensure_repo(repo_identity(root))
            parents = store.all_parents(repo_id)
            local = local_heads(root)
            heads: dict[str, str | None] = {
                name: local[name] for name in parents if name in local
            }
            missing = untried_pairs(parents, heads, store.sibling_conflicts(repo_id))
            if missing:
                store.save_sibling_conflicts(repo_id, merge_conflicts(root, missing))
        finally:
            store.close()
    except (RuntimeError, sqlite3.Error) as error:
        logger.warning(f"Could not record conflicts between open tasks: {error}")
