"""Git access for the diff review: the files that changed between two
revisions, and a file's text on either side."""

import hashlib
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
class FileDiff:
    """One changed file of ``base...head`` and a hash of its own diff: the
    same while that file's patch is, whatever else changes (see
    ``RevisionPair.file_diffs``)."""

    path: str
    #: Where a renamed file was (``None``: it was not renamed).
    old_path: str | None
    status: str
    diff_hash: str


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

    def file_diffs(self) -> list[FileDiff]:
        """Each changed file with the hash of its diff. The hash covers what the
        file's patch does (``git patch-id --stable``: not line numbers, not the
        commit, not the blobs' ids) and the file's identity (status and paths,
        since a pure rename has an empty patch), so it is unchanged by a new
        commit elsewhere or a rebase onto a moved parent that leaves the file's
        diff as it was, and changes with any edit to that diff. A rename is one
        file, as git's rename detection says."""
        found: list[FileDiff] = []
        for changed in self.changed_files():
            paths = [changed.path]
            if changed.old_path is not None:
                paths.insert(0, changed.old_path)
            patch = _git(
                self.repo, "diff", "-M", f"{self.base}...{self.head}", "--", *paths
            )
            try:
                result = logged_process.run(
                    ["git", "-C", str(self.repo), "patch-id", "--stable"],
                    input=patch,
                    capture_output=True,
                    check=True,
                )
            except subprocess.CalledProcessError as err:
                raise RevisionError(
                    err.stderr.decode(errors="replace").strip()
                ) from err
            patch_id = result.stdout.decode().split(" ", 1)[0]
            identity = "\0".join(
                [changed.status, changed.old_path or "", changed.path, patch_id]
            )
            found.append(
                FileDiff(
                    path=changed.path,
                    old_path=changed.old_path,
                    status=changed.status,
                    diff_hash=hashlib.sha256(identity.encode()).hexdigest()[:16],
                )
            )
        return found

    def text(self, side: Side, path: str) -> str:
        out = _git(self.repo, "show", f"{self.commit(side)}:{path}")
        return out.decode(errors="replace")
