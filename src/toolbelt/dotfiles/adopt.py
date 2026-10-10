"""`adopt`: move an existing real file into a repo and link it back."""

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from toolbelt.dotfiles.actions import Action, BackupDir, MakeSymlink
from toolbelt.dotfiles.manifest import RepoManifest
from toolbelt.dotfiles.plan import Plan
from toolbelt.dotfiles.secret_scan import scan_paths


@dataclass(frozen=True)
class MoveIntoRepo:
    path: Path
    src: Path

    def describe(self, backups: BackupDir) -> str:
        return f"adopt   {self.path} -> {self.src}"

    def run(self, backups: BackupDir) -> None:
        self.src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(self.path, self.src)


@dataclass(frozen=True)
class AppendManifestEntry:
    manifest_path: Path
    src: str
    dest: str
    kind: str

    def describe(self, backups: BackupDir) -> str:
        return f"manifest {self.manifest_path}: {self.src} -> {self.dest}"

    def run(self, backups: BackupDir) -> None:
        entry = (
            f"\n[[link]]\nsrc = {json.dumps(self.src)}\n"
            f"dest = {json.dumps(self.dest)}\nkind = {json.dumps(self.kind)}\n"
        )
        with self.manifest_path.open("a") as manifest:
            manifest.write(entry)


def adopt_actions(
    path: Path,
    *,
    repo: RepoManifest,
    plan: Plan,
    home: Path,
    src_rel: str | None,
) -> list[Action]:
    if path.is_symlink() or not path.exists():
        raise ValueError(f"{path} must be an existing real file or directory")
    try:
        relative_to_home = path.relative_to(home)
    except ValueError:
        raise ValueError(f"{path} is not inside {home}") from None
    if os.path.realpath(path) != os.path.join(os.path.realpath(path.parent), path.name):
        raise ValueError(f"{path} is reached through a symlinked directory")
    if path in {target.dest for target in plan.targets}:
        raise ValueError(f"{path} is already managed by a manifest")
    if repo.public:
        findings = scan_paths([path])
        if findings:
            raise ValueError(
                f"refusing to adopt into public repo {repo.root}; "
                "secret-looking content:\n" + "\n".join(map(str, findings))
            )
    src_rel = src_rel or f"{repo.adopt_dir}/{relative_to_home}"
    src = repo.root / src_rel
    if src.exists() or src.is_symlink():
        raise ValueError(f"{src} already exists in the repo")
    kind = "dir" if path.is_dir() else "file"
    return [
        MoveIntoRepo(path, src),
        AppendManifestEntry(repo.manifest_path, src_rel, f"~/{relative_to_home}", kind),
        MakeSymlink(path, src),
    ]
