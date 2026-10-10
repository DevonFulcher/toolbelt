"""The ``dotfiles.toml`` manifest at the root of a dotfiles repo.

Each repo declares which of its files land where in the home directory::

    visibility = "public"        # "public" | "private" (default private)
    adopt_dir = "Mackup"         # where `adopt` stores files; default "home"

    [[link]]
    src = "Mackup/.zshrc"        # relative to the repo root
    dest = "~/.zshrc"            # ``~/...`` or absolute
    kind = "file"                # "file" (default) | "dir" | "merge_dir"
    mode = "symlink"             # "symlink" (default) | "copy"

``merge_dir`` makes ``dest`` a real directory holding one entry per child of
``src``. Several repos can declare the same ``dest`` and each contributes its
own children, which is how ``~/.claude/skills`` is assembled from both repos.
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

MANIFEST_FILENAME = "dotfiles.toml"

type LinkKind = Literal["file", "dir", "merge_dir"]
type LinkMode = Literal["symlink", "copy"]


class LinkSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    src: str
    dest: str
    kind: LinkKind = "file"
    mode: LinkMode = "symlink"


class ManifestFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visibility: Literal["public", "private"] = "private"
    adopt_dir: str = "home"
    link: list[LinkSpec] = []


class RepoManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    root: Path
    public: bool
    adopt_dir: str
    links: list[LinkSpec]

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_FILENAME


def load_manifest(root: Path) -> RepoManifest:
    parsed = ManifestFile.model_validate(
        tomllib.loads((root / MANIFEST_FILENAME).read_text())
    )
    return RepoManifest(
        root=root,
        public=parsed.visibility == "public",
        adopt_dir=parsed.adopt_dir,
        links=parsed.link,
    )


def expand_dest(dest: str, *, home: Path) -> Path:
    """Resolve a manifest ``dest`` against an injected home directory."""
    if dest == "~":
        return home
    if dest.startswith("~/"):
        return home / dest[2:]
    path = Path(dest)
    if not path.is_absolute():
        raise ValueError(f"dest '{dest}' must be absolute or start with '~/'")
    return path
