"""The gitignored dot-files at the root of a repo checkout: ``.env``, ``.envrc``,
``.setup.sh``, ``.claude/settings.local.json``, ``.agents/...``. Hand-made
configuration that git does not carry.

``tt git append`` copies a root's dot-files into the worktree it creates
(``worktrees.copy_dotfiles``). A machine move needs the same files in two
places: the archive, so the new laptop's checkouts have them, and the worktrees
it recreates, which are made with ``git worktree add`` and so start without
them. Only ignored files are listed: a tracked dot-file is the branch's own and
a copy from the root's branch would change it.
"""

import os
import shutil
from pathlib import Path

from toolbelt import logged_process

#: Dot entries that are rebuilt, cached or machine state, never carried, at the
#: top or nested (a ``.ruff_cache`` below ``.github``); ``.coverage.*`` too.
REBUILT = frozenset(
    {
        ".git",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        ".terraform",
        ".devspace",
        ".techdocs",
        ".DS_Store",
        ".coverage",
    }
)
#: Below ``.claude`` Claude Code keeps its own checkouts of the repo.
_REBUILT_BELOW = {".claude": "worktrees"}
#: A larger file is not configuration (a log, a model, a dump).
MAX_FILE_BYTES = 256 * 1024


def _is_rebuilt(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in REBUILT for part in parts) or parts[0].startswith(".coverage."):
        return True
    return len(parts) > 1 and _REBUILT_BELOW.get(parts[0]) == parts[1]


def ignored_dotfiles(repo: Path) -> list[str]:
    """The files (and symlinks) below ``repo``'s top-level dot entries that git
    ignores and that are worth carrying, as paths relative to ``repo``."""
    result = logged_process.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--",
            ".*",
            *(f":(exclude){name}" for name in sorted(REBUILT)),
        ],
        capture_output=True,
        text=True,
    )
    found: list[str] = []
    for relative in sorted(filter(None, result.stdout.split("\0"))):
        if _is_rebuilt(relative):
            continue
        try:
            size = (repo / relative).lstat().st_size
        except OSError:
            continue
        if size <= MAX_FILE_BYTES:
            found.append(relative)
    return found


def copy_ignored_dotfiles(root: Path, worktree: Path) -> list[str]:
    """Copies ``root``'s ignored dot-files into ``worktree`` (symlinks stay
    symlinks); one that is already there is left alone. Returns the paths
    copied."""
    copied: list[str] = []
    for relative in ignored_dotfiles(root):
        source, dest = root / relative, worktree / relative
        if dest.exists() or dest.is_symlink():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), dest)
        else:
            shutil.copy2(source, dest)
        copied.append(relative)
    return copied
