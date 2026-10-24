"""``review`` in toolbelt's config file: the language servers the diff review
asks about code.

``$XDG_CONFIG_HOME/toolbelt/config.yaml`` (see ``toolbelt.git.repo_pull.
default_config_path``)::

    review:
      language_servers:
        - language: python        # the LSP language id sent with each file
          extensions: [.py]
          command: [uvx, --from, basedpyright, basedpyright-langserver, --stdio]
          settings:               # what the server is told when it asks for its settings
            python:
              pythonPath: ${repo}/.venv/bin/python   # ${repo}: the tracked repo's checkout

Without a ``review`` section the Python server above is used. A
``language_servers`` list replaces it, so list every language wanted.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

lazy import yaml


@dataclass(frozen=True)
class LanguageServer:
    language: str
    extensions: tuple[str, ...]
    command: tuple[str, ...]
    #: What the server gets for ``workspace/configuration``: by section name
    #: (``python``, or dotted: ``python.analysis``). In strings, ``${repo}``
    #: is the tracked repo's checkout.
    settings: Mapping[str, Any] = field(default_factory=dict)

    def handles(self, path: str) -> bool:
        return PurePosixPath(path).suffix in self.extensions

    def setting(self, section: str, *, repo: Path) -> Any:
        """The value under ``section`` (``None`` if there is none)."""
        value: Any = self.settings
        for key in section.split("."):
            if not isinstance(value, Mapping) or key not in value:
                return None
            value = value[key]
        return _with_repo(value, repo)


def _with_repo(value: Any, repo: Path) -> Any:
    if isinstance(value, str):
        return value.replace("${repo}", str(repo))
    if isinstance(value, Mapping):
        return {key: _with_repo(item, repo) for key, item in value.items()}
    if isinstance(value, list):
        return [_with_repo(item, repo) for item in value]
    return value


#: The server used when the config file names none. Pointing it at the repo's
#: own virtualenv lets it resolve installed packages.
PYTHON_SERVER = LanguageServer(
    language="python",
    extensions=(".py",),
    command=("uvx", "--from", "basedpyright", "basedpyright-langserver", "--stdio"),
    settings={"python": {"pythonPath": "${repo}/.venv/bin/python"}},
)


def language_servers(config_file: Path) -> tuple[LanguageServer, ...]:
    """The configured servers: the ``review.language_servers`` list, or the
    Python server when there is none."""
    if not config_file.exists():
        return (PYTHON_SERVER,)
    section = (yaml.safe_load(config_file.read_text()) or {}).get("review") or {}
    entries = section.get("language_servers")
    if entries is None:
        return (PYTHON_SERVER,)
    return tuple(
        LanguageServer(
            language=str(entry["language"]),
            extensions=tuple(str(extension) for extension in entry["extensions"]),
            command=tuple(str(part) for part in entry["command"]),
            settings=entry.get("settings") or {},
        )
        for entry in entries
    )
