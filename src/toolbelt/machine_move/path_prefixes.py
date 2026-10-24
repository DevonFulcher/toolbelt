"""Rewriting the old laptop's home (and git projects) directory to the new one's."""

import re
from dataclasses import dataclass
from pathlib import Path


def claude_project_dirname(path: str) -> str:
    """How Claude Code names a project's directory under ``~/.claude/projects``."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


@dataclass(frozen=True)
class PrefixMap:
    """Old directory prefix -> new, longest old prefix first."""

    pairs: tuple[tuple[str, str], ...]

    @classmethod
    def build(
        cls, *, old_home: str, old_workdir: str, new_home: Path, new_workdir: Path
    ) -> PrefixMap:
        pairs = {(old_workdir, str(new_workdir)), (old_home, str(new_home))}
        return cls(
            tuple(
                sorted(
                    (pair for pair in pairs if pair[0] != pair[1]),
                    key=lambda pair: len(pair[0]),
                    reverse=True,
                )
            )
        )

    def path(self, value: str) -> str:
        """``value`` with its leading old prefix replaced; any other path as is."""
        for old, new in self.pairs:
            if value == old or value.startswith(old + "/"):
                return new + value[len(old) :]
        return value

    def text(self, value: str) -> str:
        """``value`` with every old prefix replaced where it starts a path
        (``/Users/old`` but not ``/Users/older``)."""
        for old, new in self.pairs:
            value = re.sub(
                re.escape(old) + r"(?![A-Za-z0-9._-])", new.replace("\\", "\\\\"), value
            )
        return value

    def claude_project(self, dirname: str) -> str:
        """A ``~/.claude/projects`` directory name for the old prefixes, renamed
        for the new ones."""
        for old, new in self.pairs:
            old_name = claude_project_dirname(old)
            if dirname == old_name or dirname.startswith(old_name + "-"):
                return claude_project_dirname(new) + dirname[len(old_name) :]
        return dirname
