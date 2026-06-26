"""Worktree lookup for the stack workflow.

In this model every stacked branch has exactly one worktree. These helpers map
branches to their worktree paths by parsing ``git worktree list``.
"""

from pathlib import Path

from toolbelt.git.exec import run

_BRANCH_REF_PREFIX = "refs/heads/"


def worktree_paths(*, root: Path) -> dict[str, Path]:
    """Return a mapping of branch name -> worktree path for the repo.

    Detached worktrees (no branch checked out) are skipped.
    """
    result = run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=root,
        capture_output=True,
    )
    paths: dict[str, Path] = {}
    current_path: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree ") :])
        elif line.startswith("branch ") and current_path is not None:
            ref = line[len("branch ") :]
            branch = ref.removeprefix(_BRANCH_REF_PREFIX)
            paths[branch] = current_path
    return paths
