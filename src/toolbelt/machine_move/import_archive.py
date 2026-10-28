"""`tt machine import`: restore a machine archive on the new laptop.

Safe to run again: files and the DB that already match are left alone, a
differing file is first moved into ``<home>/.toolbelt/machine-import-backups/``,
and a DB that already tracks repos is kept unless ``replace_db`` says
otherwise.
"""

import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from toolbelt import logged_process
from toolbelt.machine_move import database_copy
from toolbelt.machine_move.archive_layout import (
    DATABASE_MEMBER,
    IS_PERSONAL_TRUE,
    MANIFEST_MEMBER,
    SSH_DIR,
    ArchiveManifest,
    MachineMoveError,
    Member,
)
from toolbelt.machine_move.path_prefixes import PrefixMap
from toolbelt.machine_move.repo_dotfiles import copy_ignored_dotfiles

BACKUPS_DIRNAME = "machine-import-backups"


@dataclass(frozen=True)
class ImportRequest:
    archive: Path
    home: Path
    workdir: Path
    state_dir: Path
    clone_missing: bool = True
    replace_db: bool = False
    now: Callable[[], datetime] = datetime.now


@dataclass
class ImportReport:
    done: list[str] = field(default_factory=list)
    #: Things the import could not do; each says what to do about it.
    follow_ups: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str):
    return logged_process.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


class _Importer:
    def __init__(self, request: ImportRequest) -> None:
        self.request = request
        self.report = ImportReport()
        self.backup_root = (
            request.home
            / ".toolbelt"
            / BACKUPS_DIRNAME
            / request.now().strftime("%Y%m%dT%H%M%S")
        )

    def run(self) -> ImportReport:
        request = self.request
        with zipfile.ZipFile(request.archive) as zf:
            manifest = ArchiveManifest.from_json(zf.read(MANIFEST_MEMBER).decode())
            self.prefixes = PrefixMap.build(
                old_home=manifest.old_home,
                old_workdir=manifest.old_workdir,
                new_home=request.home,
                new_workdir=request.workdir,
            )
            # The home files first (the SSH keys the clones need), then the repos
            # the DB tracks are cloned, so the files that belong inside them
            # have somewhere to go.
            for member in manifest.members:
                if member.root == "home":
                    self._restore_member(zf, member)
            repos: list[tuple[str, str | None]] = []
            if manifest.has_database:
                repos = self._restore_database(zf)
            self._clone_missing_repos(repos)
            for member in manifest.members:
                if member.root == "workdir":
                    self._restore_member(zf, member)
        for record in manifest.worktrees:
            self._restore_worktree(record.repo, record.path, record.branch)
        self._warn_if_personal()
        return self.report

    # -- files -------------------------------------------------------------

    def _destination(self, member: Member) -> Path:
        base = self.request.home if member.root == "home" else self.request.workdir
        parts = member.path.split("/")
        if member.root == "home" and parts[:2] == [".claude", "projects"]:
            parts[2] = self.prefixes.claude_project(parts[2])
        return base.joinpath(*parts)

    def _restore_member(self, zf: zipfile.ZipFile, member: Member) -> None:
        dest = self._destination(member)
        if member.root == "workdir":
            repo = self.request.workdir / member.path.split("/")[0]
            if not repo.is_dir():
                self.report.follow_ups.append(
                    f"{member.path}: {repo} is not cloned, so it was not restored; "
                    "clone it and run `tt machine import` again"
                )
                return
        data = zf.read(member.member)
        if member.rewrite_home:
            try:
                data = self.prefixes.text(data.decode()).encode()
            except UnicodeDecodeError:
                pass
        label = f"~/{member.path}" if member.root == "home" else member.path
        if dest.is_file() and not dest.is_symlink() and dest.read_bytes() == data:
            dest.chmod(member.mode)
            self.report.done.append(f"unchanged  {label}")
            return
        replaced = ""
        if dest.exists() or dest.is_symlink():
            backup = self.backup_root / member.root / member.path
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dest, backup)
            replaced = f" (previous file moved to {backup})"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if member.root == "home" and member.path.startswith(f"{SSH_DIR}/"):
            (self.request.home / SSH_DIR).chmod(0o700)
        descriptor = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        dest.chmod(member.mode)
        self.report.done.append(f"restored   {label}{replaced}")

    # -- database ----------------------------------------------------------

    def _restore_database(self, zf: zipfile.ZipFile) -> list[tuple[str, str | None]]:
        dest = self.request.state_dir / "stacks.db"
        with tempfile.TemporaryDirectory(prefix="toolbelt-machine-import-") as tmp:
            staged = Path(tmp) / "stacks.db"
            staged.write_bytes(zf.read(DATABASE_MEMBER))
            changed = database_copy.rewrite_paths(staged, self.prefixes)
            database_copy.check_integrity(staged)
            repos = database_copy.repo_rows(staged)
            if (
                dest.is_file()
                and database_copy.has_user_data(dest)
                and not self.request.replace_db
            ):
                self.report.done.append(
                    f"kept       {dest} (it already tracks repos; "
                    "`--replace-db` replaces it with the archive's)"
                )
                return repos
            self.request.state_dir.mkdir(parents=True, exist_ok=True)
            if dest.is_file():
                backup = self.backup_root / "stacks.db"
                backup.parent.mkdir(parents=True, exist_ok=True)
                database_copy.backup(dest, backup)
                self.report.done.append(f"backed up  {dest} to {backup}")
            for stale in (dest, Path(f"{dest}-wal"), Path(f"{dest}-shm")):
                stale.unlink(missing_ok=True)
            shutil.move(staged, dest)
        self.report.done.append(
            f"restored   {dest} ({len(repos)} repo(s), {changed} path(s) rewritten)"
        )
        return repos

    # -- repos and worktrees -----------------------------------------------

    def _clone_missing_repos(self, repos: list[tuple[str, str | None]]) -> None:
        for path_text, remote in repos:
            path = Path(path_text)
            if path.exists():
                continue
            if not (self.request.clone_missing and remote):
                self.follow_up_clone(path, remote, "not cloned")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            result = _git(path.parent, "clone", remote, str(path))
            if result.returncode == 0:
                self.report.done.append(f"cloned     {remote} to {path}")
            else:
                reason = (result.stderr.strip().splitlines() or ["git clone failed"])[
                    -1
                ]
                self.follow_up_clone(path, remote, reason)

    def follow_up_clone(self, path: Path, remote: str | None, reason: str) -> None:
        self.report.follow_ups.append(
            f"{path}: {reason}. `git clone {remote or '<its remote>'} {path}` "
            "(authorize the SSH key for the org's SSO first if that is why), then "
            "run `tt machine import` again"
        )

    def _restore_worktree(self, repo_text: str, path_text: str, branch: str) -> None:
        repo = Path(self.prefixes.path(repo_text))
        path = Path(self.prefixes.path(path_text))
        if path.exists():
            return
        if not repo.is_dir():
            self.report.follow_ups.append(
                f"worktree {path} ({branch}): {repo} is not cloned"
            )
            return
        _git(repo, "worktree", "prune")
        _git(repo, "fetch", "origin", branch)
        if (
            _git(
                repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
            ).returncode
            == 0
        ):
            command = ["worktree", "add", str(path), branch]
        elif (
            _git(
                repo,
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/{branch}",
            ).returncode
            == 0
        ):
            command = [
                "worktree",
                "add",
                "--track",
                "-b",
                branch,
                str(path),
                f"origin/{branch}",
            ]
        else:
            self.report.follow_ups.append(
                f"worktree {path}: branch {branch} is not on origin, so its "
                "commits were not carried (they were listed as unpushed by the export)"
            )
            return
        result = _git(repo, *command)
        if result.returncode == 0:
            self.report.done.append(f"worktree   {path} ({branch})")
            for relative in copy_ignored_dotfiles(repo, path):
                self.report.done.append(f"copied     {path}/{relative}")
        else:
            self.report.follow_ups.append(
                f"worktree {path} ({branch}): {result.stderr.strip()}"
            )

    def _warn_if_personal(self) -> None:
        machine_sh = self.request.workdir / "dotfiles" / "secrets" / "machine.sh"
        if machine_sh.is_file() and IS_PERSONAL_TRUE.search(machine_sh.read_text()):
            self.report.follow_ups.append(
                f"{machine_sh} says IS_PERSONAL=true, which makes this laptop act "
                "as a personal machine. Edit it if this is a work laptop."
            )


def import_machine(request: ImportRequest) -> ImportReport:
    if not request.archive.is_file():
        raise MachineMoveError(f"{request.archive} does not exist")
    try:
        return _Importer(request).run()
    except zipfile.BadZipFile as error:
        raise MachineMoveError(f"{request.archive} is not a machine archive") from error
