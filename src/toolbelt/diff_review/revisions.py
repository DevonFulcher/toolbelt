"""Git access for the diff review: the files that changed between two
revisions, and a file's text on either side."""

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import assert_never

from toolbelt import logged_process
from toolbelt.ui import api_models as api


class RevisionError(Exception):
    """A revision or path git does not know (shown to the user as is)."""


class Side(Enum):
    OLD = "old"
    NEW = "new"


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = logged_process.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=True
        )
    except subprocess.CalledProcessError as err:
        raise RevisionError(err.stderr.decode(errors="replace").strip()) from err
    stdout: bytes = result.stdout
    return stdout


def _known_revision(repo: Path, ref: str) -> str:
    if not ref or ref.startswith("-"):
        raise RevisionError(f"not a revision: {ref!r}")
    for candidate in (ref, f"origin/{ref}"):
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
        except RevisionError:
            continue
        return candidate
    raise RevisionError(
        f"{ref!r} is not a branch, tag or commit in {repo.name}, and there is no "
        f"origin/{ref} either: git does not have it (fetch it, or pick another revision)"
    )


@dataclass(frozen=True)
class RevisionPair:
    """``base...head``: the old side is where ``head`` forked from ``base``
    (what ``git diff base...head`` compares against), the new side is
    ``head``."""

    repo: Path
    base: str
    head: str

    def __post_init__(self) -> None:
        # A leading dash would be read by git as an option.
        for ref in (self.base, self.head):
            if not ref or ref.startswith("-"):
                raise RevisionError(f"not a revision: {ref!r}")

    @classmethod
    def resolve(cls, repo: Path, *, base: str, head: str) -> RevisionPair:
        """The pair for two names a user gave: each is a branch, tag or commit
        of the repo, or else the branch of that name on ``origin`` (a tracked
        branch is often only there)."""
        return cls(repo, _known_revision(repo, base), _known_revision(repo, head))

    def commit(self, side: Side) -> str:
        match side:
            case Side.OLD:
                out = _git(self.repo, "merge-base", self.base, self.head)
            case Side.NEW:
                out = _git(self.repo, "rev-parse", "--verify", self.head)
            case _:
                assert_never(side)
        return out.decode().strip()

    def changed_files(self) -> list[api.ReviewChangedFile]:
        out = _git(
            self.repo,
            "diff",
            "--name-status",
            "-M",
            "-z",
            f"{self.base}...{self.head}",
        )
        fields = iter(f for f in out.decode(errors="replace").split("\0") if f)
        files: list[api.ReviewChangedFile] = []
        for status in fields:
            match status[0]:
                case "R" | "C":
                    old_path = next(fields)
                    files.append(
                        api.ReviewChangedFile(
                            status="R", path=next(fields), old_path=old_path
                        )
                    )
                case "A" | "D" as kind:
                    files.append(
                        api.ReviewChangedFile(
                            status=kind, path=next(fields), old_path=None
                        )
                    )
                case _:
                    files.append(
                        api.ReviewChangedFile(
                            status="M", path=next(fields), old_path=None
                        )
                    )
        return files

    def text(self, side: Side, path: str) -> str:
        out = _git(self.repo, "show", f"{self.commit(side)}:{path}")
        return out.decode(errors="replace")
