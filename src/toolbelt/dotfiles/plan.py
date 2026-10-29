"""Resolve layered repo manifests into concrete (dest, src) targets."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

lazy from toolbelt.dotfiles.manifest import LinkMode, RepoManifest, expand_dest

# Entries starting with "." are skipped when a merge_dir is expanded: that
# matches the shell glob the old skills script used and avoids .DS_Store.
_HIDDEN_PREFIX = "."


@dataclass(frozen=True)
class Target:
    dest: Path
    src: Path
    kind: Literal["file", "dir"]
    mode: LinkMode


@dataclass(frozen=True)
class Plan:
    repos: list[RepoManifest]
    targets: list[Target]
    # merge_dir destinations, which must be real directories.
    containers: list[Path]


def build_plan(manifests: list[RepoManifest], *, home: Path) -> Plan:
    """Layer ``manifests`` in order: a later repo overrides an earlier one
    for the same destination (per child entry for ``merge_dir``)."""
    targets: dict[Path, Target] = {}
    containers: list[Path] = []
    sources = [
        (
            expand_dest(spec.src, home=home)
            if spec.src.startswith("~/")
            else manifest.root / spec.src,
            spec,
        )
        for manifest in manifests
        for spec in manifest.links
    ]
    # Home aliases need the final layered repo targets, including private
    # overrides and directories that will be installed for the first time.
    sources.sort(key=lambda item: item[1].src.startswith("~/"))
    for src, spec in sources:
        dest = expand_dest(spec.dest, home=home)
        if src == dest:
            raise ValueError(f"Source and destination must differ: {src}")
        if spec.kind == "merge_dir":
            children: dict[str, Literal["file", "dir"]] = {}
            if src.exists() or src not in containers:
                children = {
                    child.name: "dir" if child.is_dir() else "file"
                    for child in src.iterdir()
                    if not spec.src.startswith("~/") or child.exists()
                }
            children.update(
                (target.dest.name, target.kind)
                for target in targets.values()
                if target.dest.parent == src
            )
            if dest not in containers:
                containers.append(dest)
            for name, kind in sorted(children.items()):
                if name.startswith(_HIDDEN_PREFIX) or name in spec.exclude:
                    continue
                targets[dest / name] = Target(
                    dest=dest / name, src=src / name, kind=kind, mode=spec.mode
                )
        else:
            targets[dest] = Target(dest=dest, src=src, kind=spec.kind, mode=spec.mode)
    return Plan(repos=manifests, targets=list(targets.values()), containers=containers)
