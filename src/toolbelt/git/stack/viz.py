"""ASCII rendering of the stack tree.

Pure function: given the lineage map and (optionally) the current branch and
per-branch statuses, return the tree as a string. No git or network access, so
it is trivially unit-testable.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from toolbelt.git.stack.lineage import Parents, children_map, roots
from toolbelt.git.stack.status import BranchStatus, format_status

_LAST = "└─ "
_MID = "├─ "
_VERT = "│  "
_BLANK = "   "
_LOADING = "⏳ loading"


def render(
    parents: Parents,
    current: str | None = None,
    statuses: Mapping[str, BranchStatus] | None = None,
) -> str:
    """Render the stack forest, marking ``current`` with ``*``.

    Example::

        main
        └─ devon/api
           ├─ devon/api_docs
           └─ devon/api_tests *

    When ``statuses`` is given, each tracked branch (not the untracked base,
    e.g. ``main``) gets a status suffix: the branch's entry in ``statuses``
    formatted via ``format_status``, or a loading placeholder if it's not in
    ``statuses`` yet (a lookup still in flight). Pass ``None`` (the default)
    to render the plain tree with no status column at all.
    """
    children = children_map(parents)
    # (line, branch) — branch is None for the untracked base (e.g. "main"),
    # which never gets a status suffix.
    rows: list[tuple[str, str | None]] = []

    def mark(branch: str) -> str:
        return f"{branch} *" if branch == current else branch

    def walk(branch: str, prefix: str, is_last: bool, is_root: bool) -> None:
        if is_root:
            rows.append((mark(branch), branch if branch in parents else None))
            child_prefix = ""
        else:
            connector = _LAST if is_last else _MID
            rows.append((f"{prefix}{connector}{mark(branch)}", branch))
            child_prefix = prefix + (_BLANK if is_last else _VERT)
        kids = children.get(branch, [])
        for i, kid in enumerate(kids):
            walk(kid, child_prefix, i == len(kids) - 1, is_root=False)

    for base in roots(parents):
        walk(base, "", is_last=True, is_root=True)

    if statuses is None:
        return "\n".join(line for line, _ in rows)

    branch_width = max(
        (len(line) for line, branch in rows if branch is not None), default=0
    )
    # Each tracked row's status suffix, computed once so its width (for
    # aligning the PR-link column after it) and its text agree.
    suffixes = {
        branch: format_status(statuses[branch]) if branch in statuses else _LOADING
        for _, branch in rows
        if branch is not None
    }
    suffix_width = max((len(s) for s in suffixes.values()), default=0)

    lines: list[str] = []
    for line, branch in rows:
        if branch is None:
            lines.append(line)
            continue
        suffix = suffixes[branch]
        row = f"{line.ljust(branch_width)}  {suffix}"
        status = statuses.get(branch)
        if status is not None and status.url:
            row = f"{row.ljust(branch_width + 2 + suffix_width)}  {status.url}"
        lines.append(row)
    return "\n".join(lines)


@dataclass(frozen=True)
class RepoTree:
    """One repo's stack, as shown by `git tree`."""

    name: str
    path: Path
    parents: Parents
    current: str | None
    statuses: Mapping[str, BranchStatus]


def render_repos(trees: Sequence[RepoTree]) -> str:
    """Render several repos' trees, each under a ``name (path)`` header."""
    return "\n\n".join(
        f"{tree.name} ({tree.path})\n"
        + render(tree.parents, current=tree.current, statuses=tree.statuses)
        for tree in trees
    )
