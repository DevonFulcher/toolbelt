"""Filesystem changes `tt dotfiles` can make, as values that can be previewed.

Commands build a list of actions from the current state, print each one's
``describe()``, and only call ``run()`` when not in ``--dry-run``. Anything
that would be overwritten or removed is first moved (or, for ``backup``,
copied) into ``<home>/.toolbelt/dotfiles-backups/<timestamp>/``.
"""

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from toolbelt.dotfiles.plan import Plan, Target
from toolbelt.dotfiles.status import (
    State,
    container_state,
    stale_links,
    target_state,
)


class BackupDir:
    """Timestamped directory that displaced files are moved into.

    The directory is only created when something is actually stored.
    """

    def __init__(self, home: Path, now: Callable[[], datetime]) -> None:
        self._home = home
        parent = home / ".toolbelt" / "dotfiles-backups"
        root = parent / now().strftime("%Y%m%dT%H%M%S")
        suffix = 1
        while root.exists():
            root = parent / f"{now().strftime('%Y%m%dT%H%M%S')}-{suffix}"
            suffix += 1
        self.root = root

    def path_for(self, original: Path) -> Path:
        try:
            relative = original.relative_to(self._home)
        except ValueError:
            relative = original.relative_to(original.anchor)
        return self.root / relative

    def move(self, original: Path) -> None:
        stored = self.path_for(original)
        stored.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(original, stored)

    def copy(self, original: Path) -> None:
        stored = self.path_for(original)
        stored.parent.mkdir(parents=True, exist_ok=True)
        if original.is_dir() and not original.is_symlink():
            shutil.copytree(original, stored, symlinks=True)
        else:
            shutil.copy2(original, stored, follow_symlinks=False)


class Action(Protocol):
    def describe(self, backups: BackupDir) -> str: ...

    def run(self, backups: BackupDir) -> None: ...


@dataclass(frozen=True)
class MoveToBackup:
    path: Path

    def describe(self, backups: BackupDir) -> str:
        return f"backup  {self.path} -> {backups.path_for(self.path)}"

    def run(self, backups: BackupDir) -> None:
        backups.move(self.path)


@dataclass(frozen=True)
class CopyToBackup:
    path: Path

    def describe(self, backups: BackupDir) -> str:
        return f"backup  {self.path} -> {backups.path_for(self.path)} (copy)"

    def run(self, backups: BackupDir) -> None:
        backups.copy(self.path)


@dataclass(frozen=True)
class MakeDir:
    path: Path

    def describe(self, backups: BackupDir) -> str:
        return f"mkdir   {self.path}"

    def run(self, backups: BackupDir) -> None:
        self.path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class MakeSymlink:
    dest: Path
    src: Path

    def describe(self, backups: BackupDir) -> str:
        return f"link    {self.dest} -> {self.src}"

    def run(self, backups: BackupDir) -> None:
        self.dest.symlink_to(self.src)


@dataclass(frozen=True)
class MakeCopy:
    dest: Path
    src: Path

    def describe(self, backups: BackupDir) -> str:
        return f"copy    {self.src} -> {self.dest}"

    def run(self, backups: BackupDir) -> None:
        if self.src.is_dir():
            shutil.copytree(self.src, self.dest, symlinks=True)
        else:
            shutil.copy2(self.src, self.dest)


@dataclass(frozen=True)
class RemoveLink:
    path: Path

    def describe(self, backups: BackupDir) -> str:
        return f"unlink  {self.path}"

    def run(self, backups: BackupDir) -> None:
        self.path.unlink()


def _install(target: Target, *, made_dirs: set[Path]) -> list[Action]:
    actions: list[Action] = []
    parent = target.dest.parent
    if not parent.exists() and parent not in made_dirs:
        actions.append(MakeDir(parent))
        made_dirs.add(parent)
    if target.mode == "symlink":
        actions.append(MakeSymlink(target.dest, target.src))
    else:
        actions.append(MakeCopy(target.dest, target.src))
    return actions


def link_actions(plan: Plan) -> list[Action]:
    """Make every target exist, backing up whatever is in the way."""
    actions: list[Action] = []
    replaced_containers: set[Path] = set()
    made_dirs: set[Path] = set()
    for container in plan.containers:
        state = container_state(container).state
        if state is State.CONFLICT:
            actions += [MoveToBackup(container), MakeDir(container)]
            replaced_containers.add(container)
            made_dirs.add(container)
        elif state is State.MISSING:
            actions.append(MakeDir(container))
            made_dirs.add(container)
    for target in plan.targets:
        if target.dest.parent in replaced_containers:
            actions += _install(target, made_dirs=made_dirs)
            continue
        entry = target_state(target)
        if entry.state is State.OK:
            continue
        if entry.state is State.MISSING_SOURCE:
            raise FileNotFoundError(f"{target.src} (source for {target.dest})")
        if entry.state is not State.MISSING:
            actions.append(MoveToBackup(target.dest))
        actions += _install(target, made_dirs=made_dirs)
    actions += [RemoveLink(entry.dest) for entry in stale_links(plan)]
    return actions


def unlink_actions(plan: Plan) -> list[Action]:
    """Remove links that point at the repo; leave everything else alone."""
    actions: list[Action] = []
    for target in plan.targets:
        if target_state(target).state is not State.OK:
            continue
        if target.dest.is_symlink():
            actions.append(RemoveLink(target.dest))
        elif target.mode == "copy":
            actions.append(MoveToBackup(target.dest))
    actions += [RemoveLink(entry.dest) for entry in stale_links(plan)]
    return actions


def backup_actions(plan: Plan) -> list[Action]:
    """Copy aside everything `link` would displace, changing nothing."""
    actions: list[Action] = []
    for container in plan.containers:
        if container_state(container).state is State.CONFLICT:
            actions.append(CopyToBackup(container))
    for target in plan.targets:
        entry = target_state(target)
        if entry.state in (State.CONFLICT, State.WRONG_TARGET):
            actions.append(CopyToBackup(target.dest))
    return actions


def run_actions(
    actions: list[Action], *, backups: BackupDir, dry_run: bool
) -> list[str]:
    """Describe each action and, unless ``dry_run``, perform it in order."""
    lines: list[str] = []
    for action in actions:
        lines.append(action.describe(backups))
        if not dry_run:
            action.run(backups)
    return lines
