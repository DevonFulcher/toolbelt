"""Which dotfiles repos to use, from ``dotfiles.repos`` in toolbelt's config.

``$XDG_CONFIG_HOME/toolbelt/config.yaml`` (default ``~/.config/toolbelt``)::

    dotfiles:
      repos:                    # layered in order: later repos overlay earlier
        - ~/git/dotfiles
        - ~/git/dotfiles-private

Entries must be absolute or start with ``~``. This is a deliberately small
standalone reader so it can be folded into toolbelt's shared config loader.
"""

from collections.abc import Mapping
from pathlib import Path

import yaml

DEFAULT_REPOS = ("~/git/dotfiles", "~/git/dotfiles-private")


def config_path(env: Mapping[str, str], *, home: Path) -> Path:
    config_home = env.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(config_home) / "toolbelt" / "config.yaml"


def dotfiles_repos(config_file: Path, *, home: Path) -> list[Path]:
    entries: list[str] = list(DEFAULT_REPOS)
    if config_file.exists():
        config = yaml.safe_load(config_file.read_text()) or {}
        configured = (config.get("dotfiles") or {}).get("repos")
        if configured:
            entries = [str(entry) for entry in configured]
    return [_expand(entry, home=home) for entry in entries]


def _expand(entry: str, *, home: Path) -> Path:
    if entry == "~":
        return home
    if entry.startswith("~/"):
        return home / entry[2:]
    path = Path(entry)
    if not path.is_absolute():
        raise ValueError(
            f"dotfiles.repos entry '{entry}' must be absolute or start with '~'"
        )
    return path
