"""What a machine archive carries, and how it is laid out.

The archive is a zip with this layout:

``manifest.json``
    ``ArchiveManifest``: the old home and git projects directories (so the
    importer can rewrite paths), the member list and the git worktrees.
``home/<path>``
    A file that restores to ``<home>/<path>`` (SSH keys, Claude memory).
    ``bootstrap.sh`` unzips ``home/.ssh/*`` itself before anything is cloned,
    so this prefix is a contract with that script.
``workdir/<path>``
    A file that restores to ``<git projects dir>/<path>`` (the gitignored
    files of the dotfiles repos and toolbelt).
``db/stacks.db``
    A consistent copy of toolbelt's DB with the machine-specific tables emptied.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Literal

ARCHIVE_VERSION = 1
MANIFEST_MEMBER = "manifest.json"
DATABASE_MEMBER = "db/stacks.db"
DEFAULT_DOCUMENT_TITLE = "toolbelt machine archive"

type Root = Literal["home", "workdir"]

#: The repos whose gitignored files are carried, by directory name under the
#: git projects directory. ``bootstrap.sh`` clones them before the import.
DOTFILES_REPOS = ("dotfiles", "dotfiles-private", "toolbelt")

#: SSH keys carried by default: the key git signs commits with (``user.signingkey``
#: in ``~/.gitconfig``) and the identity files ssh offers to github.com (``ssh -G
#: github.com``, so ``Host github.com`` blocks in ``~/.ssh/config`` count). Each
#: comes with its ``.pub``. Restored with mode 600 (``.pub`` 644). More keys: ``tt
#: machine export --ssh-key PATH``; none: ``--no-ssh-keys``. Moving a private
#: key means the old laptop's key and the archive in 1Password both hold it:
#: revoke the old laptop's key on GitHub once the move is done.
SSH_DIR = ".ssh"

#: Tables whose rows describe the old machine only: GitHub lookups cached for
#: it, the transcript files it read, the tool calls its agents ran. They are
#: emptied in the archive's copy and refill on the new machine.
MACHINE_SPECIFIC_TABLES = (
    "transcript_files",
    "pr_lookups",
    "aggregate_prs",
    "aggregate_sync",
    "agent_tool_calls",
)

#: Columns holding absolute paths on the old machine, rewritten on import.
#: ``where`` limits the rows. A table or column the DB lacks is skipped, as
#: migrations add them over time.
PATH_COLUMNS = (
    ("repos", "path", None),
    ("repos", "git_common_dir", None),
    ("claim_spans", "target", "claim_kind = 'worktree'"),
    ("usage_calls", "cwd", None),
)


@dataclass(frozen=True)
class CarriedFiles:
    """Files matching ``pattern`` (a glob below ``root``) go in the archive.

    ``mode`` is the mode restored files get (default: as they were).
    ``rewrite_home`` rewrites the old home directory inside the file's text on
    import. ``opt_in`` names the export flag that includes the files.
    """

    root: Root
    pattern: str
    why: str
    mode: int | None = None
    rewrite_home: bool = False
    opt_in: str | None = None


def _repo_files(pattern: str, why: str, **options) -> list[CarriedFiles]:
    return [
        CarriedFiles("workdir", f"{repo}/{pattern}", why, **options)
        for repo in DOTFILES_REPOS
    ]


#: Everything carried besides the SSH keys and the DB, in one place. A path
#: that does not exist on the old laptop is skipped.
CARRIED_FILES: tuple[CarriedFiles, ...] = (
    CarriedFiles(
        "workdir",
        "dotfiles/secrets/*",
        "gitignored shell files that .zshenv and .zshrc source: tokens, "
        "machine.sh (per-machine flags such as IS_PERSONAL), toolbelt.sh (Jira)",
        mode=0o600,
        rewrite_home=True,
    ),
    *_repo_files(".env", "gitignored direnv/dotenv file", rewrite_home=True),
    *_repo_files(".envrc", "gitignored direnv file", rewrite_home=True),
    *_repo_files(".setup.sh", "gitignored worktree setup hook", rewrite_home=True),
    CarriedFiles(
        "home",
        f"{SSH_DIR}/config",
        "ssh client config (identity and host settings)",
        mode=0o600,
    ),
    CarriedFiles(
        "home",
        ".claude/projects/*/memory/**/*",
        "Claude Code memory per project (not transcripts, caches or histories)",
        rewrite_home=True,
    ),
    CarriedFiles(
        "home",
        ".zsh_history",
        "shell history",
        mode=0o600,
        opt_in="--include-history",
    ),
)

#: Ignored by git and deliberately not carried: rebuilt, cached, or regenerated.
#: Anything else that ``git status --ignored`` finds in the three repos is
#: listed by the export so a new need shows up there.
NOT_CARRIED_IGNORED = frozenset(
    {
        ".venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        ".techdocs",
        ".claude",
        ".cursor",
    }
)


#: A line of ``secrets/machine.sh`` that makes the laptop a personal one.
IS_PERSONAL_TRUE = re.compile(
    r"""^\s*(?:export\s+)?IS_PERSONAL=["']?true["']?\s*(?:#.*)?$""", re.MULTILINE
)


class MachineMoveError(Exception):
    """A failure with a message for the user; the CLI prints it, no traceback."""


@dataclass(frozen=True)
class Member:
    """One file in the archive: ``member`` is its zip name, ``root`` and
    ``path`` where it restores, relative to the root."""

    member: str
    root: Root
    path: str
    mode: int
    rewrite_home: bool


@dataclass(frozen=True)
class WorktreeRecord:
    """A git worktree on the old laptop that is not its repo's main checkout."""

    repo: str
    path: str
    branch: str


@dataclass(frozen=True)
class ArchiveManifest:
    version: int
    old_home: str
    old_workdir: str
    members: list[Member]
    worktrees: list[WorktreeRecord] = field(default_factory=list)
    has_database: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> ArchiveManifest:
        data = json.loads(text)
        if data["version"] != ARCHIVE_VERSION:
            raise MachineMoveError(
                f"archive format {data['version']} is not supported "
                f"(this toolbelt reads {ARCHIVE_VERSION}); update toolbelt"
            )
        return cls(
            version=data["version"],
            old_home=data["old_home"],
            old_workdir=data["old_workdir"],
            members=[Member(**m) for m in data["members"]],
            worktrees=[WorktreeRecord(**w) for w in data["worktrees"]],
            has_database=data["has_database"],
        )
