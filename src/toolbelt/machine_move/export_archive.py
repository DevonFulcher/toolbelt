"""`tt machine export`: everything git does not carry, in one archive."""

import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from toolbelt import logged_process
from toolbelt.machine_move import database_copy
from toolbelt.machine_move.archive_layout import (
    ARCHIVE_VERSION,
    CARRIED_FILES,
    DATABASE_MEMBER,
    DOTFILES_REPOS,
    IS_PERSONAL_TRUE,
    MANIFEST_MEMBER,
    NOT_CARRIED_IGNORED,
    SSH_DIR,
    ArchiveManifest,
    MachineMoveError,
    Member,
    Root,
)
from toolbelt.machine_move.in_flight import InFlightItem, find_in_flight
from toolbelt.machine_move.one_password import DocumentVault


@dataclass(frozen=True)
class ExportRequest:
    home: Path
    workdir: Path
    db_path: Path
    title: str
    to_file: Path | None = None
    include_history: bool = False
    #: Keys besides the signing and GitHub keys; ``None`` for no keys at all.
    ssh_keys: tuple[Path, ...] | None = ()
    force: bool = False


@dataclass(frozen=True)
class ExportReport:
    members: list[Member]
    destination: str
    warnings: list[str]
    in_flight: list[InFlightItem]
    ignored_not_carried: list[str]
    has_database: bool


def _expand(value: str, home: Path) -> Path:
    return home / value[2:] if value.startswith("~/") else Path(value)


def _ssh_identity_files(home: Path) -> list[Path]:
    config = home / SSH_DIR / "config"
    try:
        result = logged_process.run(
            [
                "ssh",
                "-G",
                "-F",
                str(config) if config.exists() else "/dev/null",
                "github.com",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    return [
        _expand(line.split(maxsplit=1)[1], home)
        for line in result.stdout.splitlines()
        if line.startswith("identityfile ")
    ]


def _signing_key(home: Path) -> Path | None:
    gitconfig = home / ".gitconfig"
    if not gitconfig.exists():
        return None
    result = logged_process.run(
        ["git", "config", "--file", str(gitconfig), "--get", "user.signingkey"],
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value or value.startswith(("key::", "ssh-")):
        return None
    path = _expand(value, home)
    return path.with_suffix("") if path.suffix == ".pub" else path


def ssh_key_files(request: ExportRequest) -> list[Path]:
    """The key files to carry: private keys and their ``.pub``, those that exist."""
    if request.ssh_keys is None:
        return []
    keys = [*request.ssh_keys]
    signing = _signing_key(request.home)
    if signing:
        keys.append(signing)
    keys.extend(_ssh_identity_files(request.home))
    files: dict[Path, None] = {}
    for key in keys:
        for candidate in (key, key.with_name(key.name + ".pub")):
            if candidate.is_file() and not candidate.is_symlink():
                files[candidate] = None
    return list(files)


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError as error:
        raise MachineMoveError(
            f"{path} is outside {base}; it cannot be carried"
        ) from error


def collect_members(request: ExportRequest) -> list[tuple[Member, Path]]:
    roots: dict[Root, Path] = {"home": request.home, "workdir": request.workdir}
    found: dict[str, tuple[Member, Path]] = {}

    def add(root: Root, path: Path, *, mode: int | None, rewrite_home: bool) -> None:
        relative = _relative(path, roots[root])
        member = f"{root}/{relative}"
        found[member] = (
            Member(
                member,
                root,
                relative,
                mode if mode is not None else stat.S_IMODE(path.stat().st_mode),
                rewrite_home,
            ),
            path,
        )

    for carried in CARRIED_FILES:
        if carried.opt_in == "--include-history" and not request.include_history:
            continue
        if carried.pattern.startswith(f"{SSH_DIR}/") and request.ssh_keys is None:
            continue
        for path in sorted(roots[carried.root].glob(carried.pattern)):
            if path.is_file() and not path.is_symlink():
                add(
                    carried.root,
                    path,
                    mode=carried.mode,
                    rewrite_home=carried.rewrite_home,
                )
    for key in ssh_key_files(request):
        add(
            "home",
            key,
            mode=0o644 if key.suffix == ".pub" else 0o600,
            rewrite_home=False,
        )
    return list(found.values())


def ignored_not_carried(workdir: Path, carried: list[Member]) -> list[str]:
    """Gitignored entries of the dotfiles repos that are neither carried nor
    known to be rebuilt (caches, virtualenvs): so a new need shows up."""
    carried_paths = {m.path for m in carried if m.root == "workdir"}
    leftovers: list[str] = []
    for repo in DOTFILES_REPOS:
        if not (workdir / repo).is_dir():
            continue
        result = logged_process.run(
            [
                "git",
                "-C",
                str(workdir / repo),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
            ],
            capture_output=True,
            text=True,
        )
        for entry in result.stdout.splitlines():
            full = f"{repo}/{entry}"
            name = entry.rstrip("/").rsplit("/", 1)[-1]
            if name in NOT_CARRIED_IGNORED:
                continue
            if any(p == full or p.startswith(full) for p in carried_paths):
                continue
            leftovers.append(full)
    return leftovers


def _personal_flag(workdir: Path) -> bool:
    machine_sh = workdir / "dotfiles" / "secrets" / "machine.sh"
    return machine_sh.is_file() and bool(
        IS_PERSONAL_TRUE.search(machine_sh.read_text())
    )


def _blockers(request: ExportRequest, db_copy: Path | None) -> list[str]:
    reasons = database_copy.blockers(db_copy) if db_copy else []
    if _personal_flag(request.workdir):
        reasons.append(
            "secrets/machine.sh says IS_PERSONAL=true: restoring it would turn the "
            "new work laptop into a personal machine (tt git append stops "
            "requiring --task, tt ui lists personal repos). Fix machine.sh on this "
            "laptop, or confirm it is right with --force"
        )
    return reasons


def export_machine(request: ExportRequest, vault: DocumentVault) -> ExportReport:
    if request.to_file is None:
        vault.ensure_signed_in()
    with tempfile.TemporaryDirectory(prefix="toolbelt-machine-export-") as tmp:
        scratch = Path(tmp)
        db_copy: Path | None = None
        if request.db_path.is_file():
            db_copy = scratch / "stacks.db"
            database_copy.backup(request.db_path, db_copy)

        warnings: list[str] = []
        blockers = _blockers(request, db_copy)
        if blockers and not request.force:
            lines = "\n".join(f"  - {reason}" for reason in blockers)
            raise MachineMoveError(
                f"refusing to export while:\n{lines}\nFinish or stop that and run "
                "this again, or pass --force to export anyway."
            )
        warnings.extend(f"--force: exporting despite: {reason}" for reason in blockers)

        repos: list[Path] = [request.workdir / name for name in DOTFILES_REPOS]
        tracked: dict[Path, list[str]] = {}
        if db_copy:
            repos += [Path(path) for path, _ in database_copy.repo_rows(db_copy)]
            for path, branch in database_copy.tracked_branches(db_copy):
                tracked.setdefault(Path(path), []).append(branch)
            database_copy.reset_for_export(db_copy)
        in_flight, worktree_records = find_in_flight(repos, tracked=tracked)

        members = collect_members(request)
        manifest = ArchiveManifest(
            version=ARCHIVE_VERSION,
            old_home=str(request.home),
            old_workdir=str(request.workdir),
            members=[member for member, _ in members],
            worktrees=worktree_records,
            has_database=db_copy is not None,
        )
        archive = scratch / "machine-archive.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_MEMBER, manifest.to_json())
            for member, source in members:
                zf.write(source, member.member)
            if db_copy:
                zf.write(db_copy, DATABASE_MEMBER)
        archive.chmod(0o600)

        if request.to_file is not None:
            request.to_file.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                request.to_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "wb") as target, archive.open("rb") as reader:
                shutil.copyfileobj(reader, target)
            request.to_file.chmod(0o600)
            destination = f"{request.to_file} (holds private keys: keep it safe)"
        else:
            destination = vault.store(archive, title=request.title)

    return ExportReport(
        members=manifest.members,
        destination=destination,
        warnings=warnings,
        in_flight=in_flight,
        ignored_not_carried=ignored_not_carried(request.workdir, manifest.members),
        has_database=manifest.has_database,
    )
