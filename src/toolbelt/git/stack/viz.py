"""ASCII rendering of the stack tree.

Pure function: given the lineage map and (optionally) the current branch, return
the tree as a string. No git access, so it is trivially unit-testable.
"""

from toolbelt.git.stack.lineage import Parents, children_map, roots

_LAST = "└─ "
_MID = "├─ "
_VERT = "│  "
_BLANK = "   "


def render(parents: Parents, current: str | None = None) -> str:
    """Render the stack forest, marking ``current`` with ``*``.

    Example::

        main
        └─ devon/api
           ├─ devon/api_docs
           └─ devon/api_tests *
    """
    children = children_map(parents)
    lines: list[str] = []

    def mark(branch: str) -> str:
        return f"{branch} *" if branch == current else branch

    def walk(branch: str, prefix: str, is_last: bool, is_root: bool) -> None:
        if is_root:
            lines.append(mark(branch))
            child_prefix = ""
        else:
            connector = _LAST if is_last else _MID
            lines.append(f"{prefix}{connector}{mark(branch)}")
            child_prefix = prefix + (_BLANK if is_last else _VERT)
        kids = children.get(branch, [])
        for i, kid in enumerate(kids):
            walk(kid, child_prefix, i == len(kids) - 1, is_root=False)

    for base in roots(parents):
        walk(base, "", is_last=True, is_root=True)

    return "\n".join(lines)
