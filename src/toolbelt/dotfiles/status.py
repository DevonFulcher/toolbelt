"""Read-only classification of every planned target against the filesystem."""

import filecmp
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from toolbelt.dotfiles.plan import Plan, Target


class State(StrEnum):
    OK = "ok"
    MISSING = "missing"
    # A real file/dir (or an unexpected kind of thing) sits at the destination.
    CONFLICT = "conflict"
    # A symlink exists but points somewhere other than the repo source.
    WRONG_TARGET = "wrong-target"
    # The manifest names a source that does not exist in the repo.
    MISSING_SOURCE = "missing-source"
    # A dangling symlink inside a merge_dir that points into a managed repo.
    STALE = "stale"


@dataclass(frozen=True)
class Entry:
    dest: Path
    state: State
    src: Path | None = None
    detail: str = ""


def link_points_to(link: Path, src: Path) -> bool:
    raw = Path(os.readlink(link))
    absolute = raw if raw.is_absolute() else link.parent / raw
    return Path(os.path.normpath(absolute)) == Path(
        os.path.normpath(src)
    ) or os.path.realpath(link) == os.path.realpath(src)


def _same_content(dest: Path, src: Path) -> bool:
    if src.is_dir():
        if not dest.is_dir():
            return False
        compared = filecmp.dircmp(dest, src)
        if compared.left_only or compared.right_only or compared.funny_files:
            return False
        _, mismatch, errors = filecmp.cmpfiles(
            dest, src, compared.common_files, shallow=False
        )
        if mismatch or errors:
            return False
        return all(_same_content(dest / d, src / d) for d in compared.common_dirs)
    return dest.is_file() and filecmp.cmp(dest, src, shallow=False)


def target_state(target: Target) -> Entry:
    dest, src = target.dest, target.src
    if not src.exists():
        return Entry(dest, State.MISSING_SOURCE, src, "source not in repo")
    if dest.is_symlink():
        if target.mode == "symlink" and link_points_to(dest, src):
            return Entry(dest, State.OK, src)
        return Entry(dest, State.WRONG_TARGET, src, f"-> {os.readlink(dest)}")
    if not dest.exists():
        return Entry(dest, State.MISSING, src)
    if target.mode == "copy":
        if _same_content(dest, src):
            return Entry(dest, State.OK, src)
        return Entry(dest, State.CONFLICT, src, "copy differs from repo")
    if os.path.samefile(dest, src):
        # Reached through a symlinked parent directory.
        return Entry(dest, State.OK, src)
    return Entry(dest, State.CONFLICT, src, "real file in the way")


def container_state(path: Path) -> Entry:
    if path.is_symlink():
        return Entry(path, State.CONFLICT, detail="symlink, must be a real dir")
    if not path.exists():
        return Entry(path, State.MISSING)
    if not path.is_dir():
        return Entry(path, State.CONFLICT, detail="not a directory")
    return Entry(path, State.OK)


def stale_links(plan: Plan) -> list[Entry]:
    """Dangling symlinks in a merge_dir that point into a managed repo."""
    planned = {target.dest for target in plan.targets}
    roots = [os.path.realpath(repo.root) for repo in plan.repos]
    stale: list[Entry] = []
    for container in plan.containers:
        if container.is_symlink() or not container.is_dir():
            continue
        for child in sorted(container.iterdir()):
            if child in planned or not child.is_symlink() or child.exists():
                continue
            target = os.path.normpath(container / os.readlink(child))
            if any(target.startswith(root + os.sep) for root in roots):
                stale.append(Entry(child, State.STALE, detail=f"-> {target}"))
    return stale


def collect_status(plan: Plan) -> list[Entry]:
    entries = [container_state(path) for path in plan.containers]
    entries += [target_state(target) for target in plan.targets]
    entries += stale_links(plan)
    return entries
