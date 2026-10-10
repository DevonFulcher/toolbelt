"""Resolve layered repo manifests into concrete (dest, src) targets."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from toolbelt.dotfiles.manifest import LinkMode, RepoManifest, expand_dest

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
    for manifest in manifests:
        for spec in manifest.links:
            src = manifest.root / spec.src
            dest = expand_dest(spec.dest, home=home)
            if spec.kind == "merge_dir":
                if dest not in containers:
                    containers.append(dest)
                for child in sorted(src.iterdir()):
                    if child.name.startswith(_HIDDEN_PREFIX):
                        continue
                    targets[dest / child.name] = Target(
                        dest=dest / child.name,
                        src=child,
                        kind="dir" if child.is_dir() else "file",
                        mode=spec.mode,
                    )
            else:
                targets[dest] = Target(
                    dest=dest, src=src, kind=spec.kind, mode=spec.mode
                )
    return Plan(repos=manifests, targets=list(targets.values()), containers=containers)
