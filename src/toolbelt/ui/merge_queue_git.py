"""The git facts the merge queue needs to merge a stacked PR safely.

When a parent PR merges, ``toolbelt git merge`` syncs its children: each gets a
merge commit that has its own old tip as first parent and the parent's squash
commit on the trunk (or, for a grandchild, the parent's new tip) as second.
Both questions here are answered from the local clone, which made those
commits; a commit it does not have answers ``False`` (so the queue drops the
PR rather than merging what it cannot vouch for).
"""

import subprocess
from pathlib import Path

from toolbelt import logged_process


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return logged_process.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )


def _is_ancestor(repo: Path, commit: str, descendant: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", commit, descendant).returncode == 0


def is_sync_merge(repo: Path, old: str, new: str, *, refs: tuple[str, ...]) -> bool:
    """``new`` is exactly what a sync adds to ``old``: a merge commit whose first
    parent is ``old`` and whose second parent is already on one of ``refs`` (the
    PR's parent branch and the trunk, as ``origin/<name>``). It brings in
    nothing that was not already on a branch the stack builds on."""
    parents = _git(repo, "rev-list", "--parents", "-n", "1", new)
    if parents.returncode != 0:
        return False
    commit, *shas = parents.stdout.split()
    if commit != new or len(shas) != 2 or shas[0] != old:
        return False
    return any(_is_ancestor(repo, shas[1], f"origin/{ref}") for ref in refs)


def contains_commit(repo: Path, commit: str, head: str) -> bool:
    """``head`` has ``commit`` in its history."""
    return _is_ancestor(repo, commit, head)


def pr_merge_state(repo: Path, number: int) -> tuple[str, str | None] | None:
    """``(state, squash commit)`` of a PR as GitHub says it (``OPEN``,
    ``MERGED``, ``CLOSED``), or ``None`` when ``gh`` cannot say."""
    viewed = logged_process.run(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "state,mergeCommit",
            "--jq",
            '[.state, .mergeCommit.oid // ""] | @tsv',
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if viewed.returncode != 0:
        return None
    state, _, commit = viewed.stdout.strip().partition("\t")
    return state, commit or None
