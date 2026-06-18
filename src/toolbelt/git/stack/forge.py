"""The single external seam: asking the code host whether a PR has merged.

Only the forge (GitHub, etc.) can authoritatively answer "did this branch's PR
land?" — a squash-merge rewrites the SHA, so local ancestry can't tell. This is
defined as a protocol and injected into `sync` so tests pass a fake instead of
shelling out to `gh`.
"""

from pathlib import Path
from typing import Protocol

from toolbelt.git.exec import run


class Forge(Protocol):
    """A code host that can report whether a branch's PR has merged."""

    def pr_is_merged(self, branch: str) -> bool: ...


class GhForge:
    """`Forge` backed by the GitHub CLI (`gh`)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def pr_is_merged(self, branch: str) -> bool:
        # Query by head branch: the PR record persists even after the remote
        # branch is deleted on merge. A non-empty merged list means it landed.
        result = run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "merged",
                "--json",
                "number",
                "--jq",
                "length",
            ],
            cwd=self._root,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            return False
        return result.stdout.strip() not in ("", "0")
