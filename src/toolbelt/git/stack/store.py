"""SQLite persistence for stack metadata (which branch is stacked on which).

One database holds every repo toolbelt knows about, so a future UI and
multi-repo views can read it without touching each repo's git config.

Schema versioning uses ``PRAGMA user_version`` plus the ordered ``_MIGRATIONS``
list below: migration ``i`` takes the database from version ``i`` to ``i + 1``.
To change the schema, append a migration — never edit an existing one.

Rows are read through ``sqlite3.Row`` (columns by name, never by position)
and handed out as the frozen dataclasses below. The SQL stays hand-written
here.

Several agents write concurrently, so connections use WAL + a busy timeout and
every multi-statement change runs in a ``BEGIN IMMEDIATE`` transaction.

This module is pure sqlite: it knows nothing about git. Callers resolve a
repo's identity (see ``lineage``) and pass it in.
"""

import contextlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

STATE_DIR_ENV_VAR = "TOOLBELT_HOME"
DB_FILENAME = "stacks.db"
_BUSY_TIMEOUT_MS = 30_000

Parents = dict[str, str]

_MIGRATIONS: list[tuple[str, ...]] = [
    (
        # A repo is identified by its canonical git common dir, which is shared
        # by all of its worktrees. ``imported_at`` was set when legacy
        # ``toolbelt-stack.*`` git config was first copied in; it is no longer
        # written (legacy config is re-checked, see ``lineage``).
        """
        CREATE TABLE repos (
            id INTEGER PRIMARY KEY,
            git_common_dir TEXT NOT NULL UNIQUE,
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            remote_url TEXT,
            imported_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        # ``base`` is reserved (nullable, currently unset). Later versions add
        # task / Claude-session link columns via new migrations.
        """
        CREATE TABLE branches (
            id INTEGER PRIMARY KEY,
            repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            parent TEXT NOT NULL,
            base TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (repo_id, name)
        )
        """,
    ),
    (
        # Links to the work a branch is for: a Jira ticket (key or URL) and a
        # deep link to the Claude Desktop session/chat. Nullable, since
        # branches created before this had none.
        "ALTER TABLE branches ADD COLUMN jira TEXT",
        "ALTER TABLE branches ADD COLUMN agent_link TEXT",
    ),
    (
        # Mirroring to GitHub's native stacked PRs (see ``gh_stack``).
        # ``gh_stack_unsupported_at`` is when `gh stack link` last reported the
        # feature missing for the repo (disabled, or the extension is not
        # installed); retried after a TTL. ``published_chains`` holds, per
        # chain bottom, the ordered branch list last linked and its hash, so an
        # unchanged chain costs no GitHub call.
        "ALTER TABLE repos ADD COLUMN gh_stack_unsupported_at TEXT",
        """
        CREATE TABLE published_chains (
            repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            bottom TEXT NOT NULL,
            chain_hash TEXT NOT NULL,
            branches TEXT NOT NULL,
            published_at TEXT NOT NULL,
            PRIMARY KEY (repo_id, bottom)
        )
        """,
    ),
    (
        # Generalise the Jira link into a tracker-agnostic task reference.
        # ``task`` keeps the raw ref as given (key or URL); ``task_tracker``
        # names the tracker it belongs to (e.g. ``jira``) and is NULL when it
        # was unknown. Every existing ref was a Jira one.
        "ALTER TABLE branches RENAME COLUMN jira TO task",
        "ALTER TABLE branches ADD COLUMN task_tracker TEXT",
        "UPDATE branches SET task_tracker = 'jira' WHERE task IS NOT NULL",
    ),
    (
        # When the task began: set once, when the branch is first registered
        # (never changed by reparenting). Existing rows are backfilled from
        # ``created_at`` (for ones imported from legacy git config that is
        # the import time).
        "ALTER TABLE branches ADD COLUMN started_at TEXT",
        "UPDATE branches SET started_at = created_at",
    ),
    (
        # A branch's task facts, kept when it stops being tracked (landed,
        # cleaned up, `tt remove`): the work history the UI's aggregate view
        # is built from. Written by ``remove_branch``; ``landed`` says the
        # removal was the cleanup after a merge. Rows exist only from this
        # migration on (``archive_info.since``): earlier removals are not
        # recoverable.
        """
        CREATE TABLE removed_branches (
            id INTEGER PRIMARY KEY,
            repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            task TEXT,
            task_tracker TEXT,
            started_at TEXT,
            removed_at TEXT NOT NULL,
            landed INTEGER NOT NULL
        )
        """,
        "CREATE INDEX removed_branches_by_repo ON removed_branches (repo_id)",
        "CREATE TABLE archive_info (since TEXT NOT NULL)",
        (
            "INSERT INTO archive_info (since) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))"
        ),
    ),
    (
        # Identity, so the aggregate view can join tasks to PRs by id rather
        # than by branch name (a name is reused). ``task_id`` is stable for
        # the life of a task and is carried into ``removed_branches`` when
        # the branch stops being tracked; existing rows get a random one.
        # ``pr_number`` / ``pr_node_id`` (GitHub's GraphQL node id) are the
        # task's PR, recorded by the CLI flows that see it (``link_pr``) and
        # NULL until then; the repo is the row's repo (its remote).
        "ALTER TABLE branches ADD COLUMN task_id TEXT",
        "ALTER TABLE branches ADD COLUMN pr_number INTEGER",
        "ALTER TABLE branches ADD COLUMN pr_node_id TEXT",
        "UPDATE branches SET task_id = lower(hex(randomblob(16)))",
        "CREATE UNIQUE INDEX branches_by_task_id ON branches (task_id)",
        "ALTER TABLE removed_branches ADD COLUMN task_id TEXT",
        "ALTER TABLE removed_branches ADD COLUMN pr_number INTEGER",
        "ALTER TABLE removed_branches ADD COLUMN pr_node_id TEXT",
        "UPDATE removed_branches SET task_id = lower(hex(randomblob(16)))",
    ),
    (
        # What claims were held, when: one row per (holder, claim) while it
        # stays continuously live, written by the claims reader as it reads
        # the claim files (which are overwritten every turn and expire).
        # ``first_seen`` is the claim's own ``touched_at`` when that is
        # earlier than the first read. ``task_id`` is the tracked task the
        # claim resolved to; NULL when it matched none. ``holder_kind`` is
        # the claim file's ``kind``.
        """
        CREATE TABLE claim_spans (
            id INTEGER PRIMARY KEY,
            holder TEXT NOT NULL,
            session_id TEXT,
            agent_id TEXT,
            holder_kind TEXT NOT NULL,
            claim_kind TEXT NOT NULL,
            target TEXT NOT NULL,
            task_id TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """,
        "CREATE INDEX claim_spans_by_holder ON claim_spans (holder, last_seen)",
        "CREATE INDEX claim_spans_by_session ON claim_spans (session_id, first_seen)",
    ),
    (
        # Model API calls read from Claude Code's session transcripts (see
        # ``token_spend``): one row per API response, keyed by its message id
        # (a transcript repeats one response's usage on every row it
        # writes). ``usage_json`` is the raw usage object the cost was
        # priced from, and ``cost_usd`` is NULL while no price is known for
        # ``model``. ``transcript_files.offset`` is how far each append-only
        # transcript has been read.
        """
        CREATE TABLE usage_calls (
            message_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            agent_id TEXT,
            model TEXT NOT NULL,
            at TEXT NOT NULL,
            cwd TEXT,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cache_read_tokens INTEGER NOT NULL,
            cache_write_5m_tokens INTEGER NOT NULL,
            cache_write_1h_tokens INTEGER NOT NULL,
            usage_json TEXT NOT NULL,
            cost_usd REAL
        )
        """,
        "CREATE INDEX usage_calls_by_time ON usage_calls (at)",
        "CREATE INDEX usage_calls_by_session ON usage_calls (session_id, at)",
        """
        CREATE TABLE transcript_files (
            path TEXT PRIMARY KEY,
            offset INTEGER NOT NULL
        )
        """,
    ),
]


@dataclass(frozen=True)
class RepoIdentity:
    git_common_dir: Path
    path: Path
    remote_url: str | None


@dataclass(frozen=True)
class BranchLinks:
    task: str | None
    task_tracker: str | None
    agent_link: str | None


@dataclass(frozen=True)
class PrLink:
    """A PR's identity: its number in the repo and GitHub's node id (which,
    unlike the number, is globally unique and survives a repo transfer)."""

    number: int
    node_id: str


@dataclass(frozen=True)
class PrCandidate:
    """A PR the code host reports for a branch name, offered to ``link_pr``
    in order of preference. ``created_at`` keeps an earlier task's PR (same
    branch name) from being linked to a later task."""

    link: PrLink
    created_at: datetime


@dataclass(frozen=True)
class RemovedBranch:
    """A branch that was tracked and no longer is, with the task facts it
    had. ``landed``: removed as the cleanup after its PR merged.
    ``task_id`` is the id it had while tracked; ``pr`` its PR when that was
    ever recorded."""

    task_id: str
    pr: PrLink | None
    repo_id: int
    name: str
    task: str | None
    task_tracker: str | None
    started_at: datetime | None
    removed_at: datetime
    landed: bool


@dataclass(frozen=True)
class TrackedBranchFacts:
    """A branch that is tracked now, with the facts the aggregate view uses."""

    task_id: str
    pr: PrLink | None
    repo_id: int
    name: str
    task: str | None
    started_at: datetime | None


@dataclass(frozen=True)
class ClaimSpan:
    """A claim held continuously from ``first_seen`` to ``last_seen``."""

    holder: str
    session_id: str | None
    agent_id: str | None
    holder_kind: str
    claim_kind: str
    target: str
    task_id: str | None
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class TokenCounts:
    """Tokens of one kind of work, as the API reports them: ``input`` is the
    uncached input, so the four input kinds do not overlap."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0

    @property
    def total(self) -> int:
        return (
            self.input
            + self.output
            + self.cache_read
            + self.cache_write_5m
            + self.cache_write_1h
        )


@dataclass(frozen=True)
class UsageCall:
    """One API response read from a session transcript."""

    message_id: str
    session_id: str
    #: The subagent that made it; None for the session's own calls.
    agent_id: str | None
    model: str
    at: datetime
    cwd: str | None
    tokens: TokenCounts
    #: The raw usage object, kept so a call can be priced again later.
    usage: dict
    #: None when no price was known for ``model`` yet.
    cost_usd: float | None


@dataclass(frozen=True)
class ModelUsage:
    model: str
    calls: int
    tokens: TokenCounts
    #: Cost of the priced calls only.
    cost_usd: float
    unpriced_calls: int


@dataclass(frozen=True)
class TaskUsage:
    """Spend attributed to one task through claims (see
    ``StackStore.usage_by_task``); ``task_id`` None is the spend no claim
    covers. A call that two tasks' claims cover at once is split evenly, so
    the figures are fractional."""

    task_id: str | None
    calls: float
    tokens: float
    cost_usd: float
    unpriced_calls: float


@dataclass(frozen=True)
class PublishedChain:
    chain_hash: str
    branches: list[str]


@dataclass(frozen=True)
class RepoRecord:
    id: int
    name: str
    path: Path
    git_common_dir: Path
    #: ``origin``'s URL when the repo was registered.
    remote_url: str | None = None


def state_dir() -> Path:
    """toolbelt's state dir: ``$TOOLBELT_HOME`` or ``~/.toolbelt``."""
    override = os.environ.get(STATE_DIR_ENV_VAR)
    return Path(override) if override else Path.home() / ".toolbelt"


def default_db_path() -> Path:
    return state_dir() / DB_FILENAME


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _iso(moment: datetime) -> str:
    """``moment`` as UTC text that sorts as time does (fixed width)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _new_task_id() -> str:
    return uuid.uuid4().hex


def _pr_link(row: sqlite3.Row) -> PrLink | None:
    """The PR link in a row's ``pr_number`` / ``pr_node_id`` columns."""
    if row["pr_node_id"] is None or row["pr_number"] is None:
        return None
    return PrLink(number=row["pr_number"], node_id=row["pr_node_id"])


def _optional_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class StackStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, transactions are explicit.
        self._conn = sqlite3.connect(
            db_path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def data_version(self) -> int:
        """Changes whenever another connection commits to the database, so a
        long-lived store can tell that something was written."""
        return self._conn.execute("PRAGMA data_version").fetchone()["data_version"]

    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()["user_version"]

    def _migrate(self) -> None:
        if self.schema_version() == len(_MIGRATIONS):
            return
        with self._write() as conn:
            # Re-read inside the write lock: another process may have migrated.
            for index in range(self.schema_version(), len(_MIGRATIONS)):
                for statement in _MIGRATIONS[index]:
                    conn.execute(statement)
                # PRAGMA can't take bound parameters.
                conn.execute(f"PRAGMA user_version = {index + 1}")

    def ensure_repo(self, identity: RepoIdentity) -> int:
        """Return the repo's id, registering it if it is new."""
        with self._write() as conn:
            row = conn.execute(
                "SELECT id FROM repos WHERE git_common_dir = ?",
                (str(identity.git_common_dir),),
            ).fetchone()
            if row is not None:
                return row["id"]
            cursor = conn.execute(
                "INSERT INTO repos "
                "(git_common_dir, path, name, remote_url, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(identity.git_common_dir),
                    str(identity.path),
                    identity.path.name,
                    identity.remote_url,
                    _now(),
                ),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def import_parents(self, repo_id: int, parents: Parents) -> None:
        """Add ``parents`` for branches the repo does not already track.

        Existing rows win: an imported parent never overwrites one already in
        the database.
        """
        now = _now()
        with self._write() as conn:
            for branch, parent in parents.items():
                conn.execute(
                    "INSERT OR IGNORE INTO branches "
                    "(task_id, repo_id, name, parent, started_at, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_new_task_id(), repo_id, branch, parent, now, now, now),
                )

    def get_parent(self, repo_id: int, branch: str) -> str | None:
        row = self._conn.execute(
            "SELECT parent FROM branches WHERE repo_id = ? AND name = ?",
            (repo_id, branch),
        ).fetchone()
        return row["parent"] if row else None

    def set_parent(
        self,
        repo_id: int,
        branch: str,
        parent: str,
        *,
        task: str | None = None,
        task_tracker: str | None = None,
        agent_link: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        """Record ``parent``; ``task``/``agent_link`` are only written when
        given, so repointing a branch never clears its links. ``task_tracker``
        names the tracker ``task`` belongs to and is written along with it.
        ``started_at`` (default: now) is only used when the branch is first
        inserted: an existing row keeps its own."""
        now = _now()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO branches "
                "(task_id, repo_id, name, parent, task, task_tracker, "
                "agent_link, started_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (repo_id, name) DO UPDATE SET "
                "parent = excluded.parent, "
                "task_tracker = CASE WHEN excluded.task IS NULL "
                "THEN task_tracker ELSE excluded.task_tracker END, "
                "task = COALESCE(excluded.task, task), "
                "agent_link = COALESCE(excluded.agent_link, agent_link), "
                "updated_at = excluded.updated_at",
                (
                    _new_task_id(),
                    repo_id,
                    branch,
                    parent,
                    task,
                    task_tracker,
                    agent_link,
                    started_at.isoformat() if started_at else now,
                    now,
                    now,
                ),
            )

    def get_started_at(self, repo_id: int, branch: str) -> datetime | None:
        """When ``branch``'s task started, or ``None`` if the branch is not
        tracked."""
        row = self._conn.execute(
            "SELECT started_at FROM branches WHERE repo_id = ? AND name = ?",
            (repo_id, branch),
        ).fetchone()
        if row is None:
            return None
        return _optional_time(row["started_at"])

    def get_links(self, repo_id: int, branch: str) -> BranchLinks | None:
        """``branch``'s links, or ``None`` if the branch is not tracked."""
        row = self._conn.execute(
            "SELECT task, task_tracker, agent_link FROM branches "
            "WHERE repo_id = ? AND name = ?",
            (repo_id, branch),
        ).fetchone()
        if row is None:
            return None
        return BranchLinks(
            task=row["task"],
            task_tracker=row["task_tracker"],
            agent_link=row["agent_link"],
        )

    def set_links(
        self,
        repo_id: int,
        branch: str,
        *,
        task: str | None = None,
        task_tracker: str | None = None,
        agent_link: str | None = None,
    ) -> bool:
        """Update the given links on a tracked branch (omitted ones are left
        alone). Returns ``False`` if the branch is not tracked."""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE branches SET "
                "task_tracker = CASE WHEN ? IS NULL THEN task_tracker ELSE ? END, "
                "task = COALESCE(?, task), "
                "agent_link = COALESCE(?, agent_link), updated_at = ? "
                "WHERE repo_id = ? AND name = ?",
                (task, task_tracker, task, agent_link, _now(), repo_id, branch),
            )
            return cursor.rowcount > 0

    def remove_branch(self, repo_id: int, branch: str, *, landed: bool = False) -> None:
        """Stop tracking ``branch``, first archiving its task facts (see
        ``removed_branches``) in the same transaction. ``landed``: its PR was
        merged and this is the cleanup after it."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO removed_branches "
                "(task_id, pr_number, pr_node_id, repo_id, name, task, "
                "task_tracker, started_at, removed_at, landed) "
                "SELECT task_id, pr_number, pr_node_id, repo_id, name, task, "
                "task_tracker, started_at, ?, ? "
                "FROM branches WHERE repo_id = ? AND name = ?",
                (_now(), int(landed), repo_id, branch),
            )
            conn.execute(
                "DELETE FROM branches WHERE repo_id = ? AND name = ?",
                (repo_id, branch),
            )

    def get_pr_link(self, repo_id: int, branch: str) -> PrLink | None:
        """The PR recorded for ``branch``'s task, if any."""
        row = self._conn.execute(
            "SELECT pr_number, pr_node_id FROM branches WHERE repo_id = ? AND name = ?",
            (repo_id, branch),
        ).fetchone()
        return _pr_link(row) if row else None

    def link_pr(
        self, repo_id: int, branch: str, candidates: Sequence[PrCandidate]
    ) -> PrLink | None:
        """Record which PR is ``branch``'s task's, when it first becomes
        known, and return the task's link (``None``: untracked, or no
        candidate qualified).

        A recorded link is never replaced. Otherwise the first candidate (the
        caller's order of preference) that is not another task's PR (already
        linked from a tracked or archived row of this repo) and was not
        created before this branch name's last archived task ended (it is
        that task's PR, from before the name was reused) is taken."""
        with self._write() as conn:
            row = conn.execute(
                "SELECT pr_number, pr_node_id FROM branches "
                "WHERE repo_id = ? AND name = ?",
                (repo_id, branch),
            ).fetchone()
            if row is None:
                return None
            existing = _pr_link(row)
            if existing is not None:
                return existing
            claimed = {
                r["pr_node_id"]
                for r in conn.execute(
                    "SELECT pr_node_id FROM branches WHERE repo_id = ? "
                    "UNION SELECT pr_node_id FROM removed_branches "
                    "WHERE repo_id = ?",
                    (repo_id, repo_id),
                )
            }
            last_removed = conn.execute(
                "SELECT removed_at FROM removed_branches "
                "WHERE repo_id = ? AND name = ? ORDER BY removed_at DESC LIMIT 1",
                (repo_id, branch),
            ).fetchone()
            earliest = (
                datetime.fromisoformat(last_removed["removed_at"])
                if last_removed
                else None
            )
            for candidate in candidates:
                if candidate.link.node_id in claimed:
                    continue
                if earliest is not None and candidate.created_at < earliest:
                    continue
                conn.execute(
                    "UPDATE branches SET pr_number = ?, pr_node_id = ? "
                    "WHERE repo_id = ? AND name = ?",
                    (candidate.link.number, candidate.link.node_id, repo_id, branch),
                )
                return candidate.link
            return None

    def removed_branches(self) -> list[RemovedBranch]:
        """Every archived branch, oldest removal first."""
        rows = self._conn.execute(
            "SELECT task_id, pr_number, pr_node_id, repo_id, name, task, "
            "task_tracker, started_at, removed_at, landed "
            "FROM removed_branches ORDER BY removed_at, id"
        )
        return [
            RemovedBranch(
                task_id=r["task_id"],
                pr=_pr_link(r),
                repo_id=r["repo_id"],
                name=r["name"],
                task=r["task"],
                task_tracker=r["task_tracker"],
                started_at=_optional_time(r["started_at"]),
                removed_at=datetime.fromisoformat(r["removed_at"]),
                landed=bool(r["landed"]),
            )
            for r in rows
        ]

    def tracked_branch_facts(self) -> list[TrackedBranchFacts]:
        rows = self._conn.execute(
            "SELECT task_id, pr_number, pr_node_id, repo_id, name, task, "
            "started_at FROM branches ORDER BY id"
        )
        return [
            TrackedBranchFacts(
                task_id=r["task_id"],
                pr=_pr_link(r),
                repo_id=r["repo_id"],
                name=r["name"],
                task=r["task"],
                started_at=_optional_time(r["started_at"]),
            )
            for r in rows
        ]

    def record_claim_seen(
        self,
        *,
        holder: str,
        session_id: str | None,
        agent_id: str | None,
        holder_kind: str,
        claim_kind: str,
        target: str,
        task_id: str | None,
        touched_at: datetime,
        seen_at: datetime,
        max_gap: timedelta,
    ) -> None:
        """Note that ``holder`` held this claim at ``seen_at``. Extends the
        holder's latest span for the same claim when it was last seen within
        ``max_gap``; otherwise starts a new one (at ``touched_at`` when that
        is earlier than ``seen_at``)."""
        seen = seen_at.astimezone(UTC)
        with self._write() as conn:
            row = conn.execute(
                "SELECT id, last_seen FROM claim_spans WHERE holder = ? "
                "AND claim_kind = ? AND target = ? AND task_id IS ? "
                "ORDER BY id DESC LIMIT 1",
                (holder, claim_kind, target, task_id),
            ).fetchone()
            if (
                row is not None
                and datetime.fromisoformat(row["last_seen"]) >= seen - max_gap
            ):
                conn.execute(
                    "UPDATE claim_spans SET last_seen = ? WHERE id = ?",
                    (
                        _iso(max(datetime.fromisoformat(row["last_seen"]), seen)),
                        row["id"],
                    ),
                )
                return
            conn.execute(
                "INSERT INTO claim_spans (holder, session_id, agent_id, "
                "holder_kind, claim_kind, target, task_id, first_seen, "
                "last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    holder,
                    session_id,
                    agent_id,
                    holder_kind,
                    claim_kind,
                    target,
                    task_id,
                    _iso(min(touched_at, seen)),
                    _iso(seen),
                ),
            )

    def claim_spans(self, *, since: datetime | None = None) -> list[ClaimSpan]:
        """Every recorded span, or those last seen at or after ``since``,
        oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM claim_spans WHERE last_seen >= ? ORDER BY first_seen, id",
            (_iso(since or datetime.min.replace(tzinfo=UTC)),),
        )
        return [
            ClaimSpan(
                holder=r["holder"],
                session_id=r["session_id"],
                agent_id=r["agent_id"],
                holder_kind=r["holder_kind"],
                claim_kind=r["claim_kind"],
                target=r["target"],
                task_id=r["task_id"],
                first_seen=datetime.fromisoformat(r["first_seen"]),
                last_seen=datetime.fromisoformat(r["last_seen"]),
            )
            for r in rows
        ]

    def transcript_offset(self, path: str) -> int:
        """How many bytes of the transcript at ``path`` have been read."""
        row = self._conn.execute(
            "SELECT offset FROM transcript_files WHERE path = ?", (path,)
        ).fetchone()
        return row["offset"] if row else 0

    def add_usage_calls(
        self, calls: Sequence[UsageCall], *, transcript: str, offset: int
    ) -> int:
        """Store ``calls`` read from ``transcript`` and note that it has been
        read up to ``offset``, in one transaction. A transcript writes one
        response on several rows, the first often with only part of the
        usage, so a call already stored is replaced when the new one has more
        tokens. Returns how many calls were not stored before."""
        with self._write() as conn:
            before = conn.execute("SELECT COUNT(*) FROM usage_calls").fetchone()[0]
            for call in calls:
                conn.execute(
                    "INSERT INTO usage_calls (message_id, session_id, agent_id, "
                    "model, at, cwd, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_write_5m_tokens, "
                    "cache_write_1h_tokens, usage_json, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (message_id) DO UPDATE SET "
                    "input_tokens = excluded.input_tokens, "
                    "output_tokens = excluded.output_tokens, "
                    "cache_read_tokens = excluded.cache_read_tokens, "
                    "cache_write_5m_tokens = excluded.cache_write_5m_tokens, "
                    "cache_write_1h_tokens = excluded.cache_write_1h_tokens, "
                    "usage_json = excluded.usage_json, "
                    "cost_usd = excluded.cost_usd "
                    "WHERE excluded.input_tokens + excluded.output_tokens "
                    "+ excluded.cache_read_tokens + excluded.cache_write_5m_tokens "
                    "+ excluded.cache_write_1h_tokens > usage_calls.input_tokens "
                    "+ usage_calls.output_tokens + usage_calls.cache_read_tokens "
                    "+ usage_calls.cache_write_5m_tokens "
                    "+ usage_calls.cache_write_1h_tokens",
                    (
                        call.message_id,
                        call.session_id,
                        call.agent_id,
                        call.model,
                        _iso(call.at),
                        call.cwd,
                        call.tokens.input,
                        call.tokens.output,
                        call.tokens.cache_read,
                        call.tokens.cache_write_5m,
                        call.tokens.cache_write_1h,
                        json.dumps(call.usage, sort_keys=True),
                        call.cost_usd,
                    ),
                )
            conn.execute(
                "INSERT INTO transcript_files (path, offset) VALUES (?, ?) "
                "ON CONFLICT (path) DO UPDATE SET offset = excluded.offset",
                (transcript, offset),
            )
            return (
                conn.execute("SELECT COUNT(*) FROM usage_calls").fetchone()[0] - before
            )

    def unpriced_models(self) -> dict[str, int]:
        """Each model with calls that have no cost yet, and how many."""
        rows = self._conn.execute(
            "SELECT model, COUNT(*) AS calls FROM usage_calls "
            "WHERE cost_usd IS NULL GROUP BY model ORDER BY model"
        )
        return {r["model"]: r["calls"] for r in rows}

    def unpriced_usage(self, model: str) -> list[tuple[str, dict]]:
        """``(message_id, usage)`` of ``model``'s calls that have no cost."""
        rows = self._conn.execute(
            "SELECT message_id, usage_json FROM usage_calls "
            "WHERE model = ? AND cost_usd IS NULL",
            (model,),
        )
        return [(r["message_id"], json.loads(r["usage_json"])) for r in rows]

    def set_usage_costs(self, costs: dict[str, float]) -> None:
        """Set the cost of the calls with these message ids."""
        with self._write() as conn:
            conn.executemany(
                "UPDATE usage_calls SET cost_usd = ? WHERE message_id = ?",
                [(cost, message_id) for message_id, cost in costs.items()],
            )

    def usage_by_model(self, start: datetime, end: datetime) -> list[ModelUsage]:
        """Calls made in ``[start, end)``, per model, biggest spend first."""
        rows = self._conn.execute(
            "SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS input, "
            "SUM(output_tokens) AS output, SUM(cache_read_tokens) AS cache_read, "
            "SUM(cache_write_5m_tokens) AS cache_write_5m, "
            "SUM(cache_write_1h_tokens) AS cache_write_1h, "
            "SUM(COALESCE(cost_usd, 0)) AS cost, "
            "SUM(cost_usd IS NULL) AS unpriced FROM usage_calls "
            "WHERE at >= ? AND at < ? GROUP BY model ORDER BY cost DESC, model",
            (_iso(start), _iso(end)),
        )
        return [
            ModelUsage(
                model=r["model"],
                calls=r["calls"],
                tokens=TokenCounts(
                    input=r["input"],
                    output=r["output"],
                    cache_read=r["cache_read"],
                    cache_write_5m=r["cache_write_5m"],
                    cache_write_1h=r["cache_write_1h"],
                ),
                cost_usd=r["cost"],
                unpriced_calls=r["unpriced"],
            )
            for r in rows
        ]

    def usage_by_task(self, start: datetime, end: datetime) -> list[TaskUsage]:
        """Calls made in ``[start, end)`` attributed to tasks: a call belongs
        to each task that a claim span of the call's session covers at the
        call's time (split evenly when several do). Calls with no such claim
        are one row with ``task_id`` None. Biggest spend first."""
        rows = self._conn.execute(
            """
            WITH calls AS (
                SELECT message_id, session_id, at, cost_usd,
                       input_tokens + output_tokens + cache_read_tokens
                       + cache_write_5m_tokens + cache_write_1h_tokens AS tokens
                FROM usage_calls WHERE at >= ? AND at < ?
            ),
            claimed AS (
                SELECT DISTINCT c.message_id, s.task_id
                FROM calls c JOIN claim_spans s
                  ON s.session_id = c.session_id AND s.task_id IS NOT NULL
                 AND c.at BETWEEN s.first_seen AND s.last_seen
            ),
            weights AS (
                SELECT message_id, task_id,
                       1.0 / COUNT(*) OVER (PARTITION BY message_id) AS weight
                FROM claimed
            )
            SELECT w.task_id AS task_id, SUM(w.weight) AS calls,
                   SUM(w.weight * c.tokens) AS tokens,
                   SUM(w.weight * COALESCE(c.cost_usd, 0)) AS cost,
                   SUM(w.weight * (c.cost_usd IS NULL)) AS unpriced
            FROM weights w JOIN calls c USING (message_id) GROUP BY w.task_id
            UNION ALL
            SELECT NULL, COUNT(*), COALESCE(SUM(tokens), 0),
                   COALESCE(SUM(COALESCE(cost_usd, 0)), 0),
                   COALESCE(SUM(cost_usd IS NULL), 0)
            FROM calls WHERE message_id NOT IN (SELECT message_id FROM claimed)
            HAVING COUNT(*) > 0
            ORDER BY cost DESC
            """,
            (_iso(start), _iso(end)),
        )
        return [
            TaskUsage(
                task_id=r["task_id"],
                calls=r["calls"],
                tokens=r["tokens"],
                cost_usd=r["cost"],
                unpriced_calls=r["unpriced"],
            )
            for r in rows
        ]

    def archive_since(self) -> datetime:
        """When archiving began: earlier removals are not in the archive."""
        row = self._conn.execute("SELECT since FROM archive_info").fetchone()
        return datetime.fromisoformat(row["since"])

    def all_parents(self, repo_id: int) -> Parents:
        rows = self._conn.execute(
            "SELECT name, parent FROM branches WHERE repo_id = ?", (repo_id,)
        )
        return {r["name"]: r["parent"] for r in rows}

    def gh_stack_unsupported_at(self, repo_id: int) -> datetime | None:
        row = self._conn.execute(
            "SELECT gh_stack_unsupported_at FROM repos WHERE id = ?", (repo_id,)
        ).fetchone()
        return _optional_time(row["gh_stack_unsupported_at"])

    def set_gh_stack_unsupported_at(self, repo_id: int, at: datetime | None) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE repos SET gh_stack_unsupported_at = ? WHERE id = ?",
                (at.isoformat() if at else None, repo_id),
            )

    def get_published_chain(self, repo_id: int, bottom: str) -> PublishedChain | None:
        row = self._conn.execute(
            "SELECT chain_hash, branches FROM published_chains "
            "WHERE repo_id = ? AND bottom = ?",
            (repo_id, bottom),
        ).fetchone()
        if row is None:
            return None
        return PublishedChain(row["chain_hash"], row["branches"].split("\n"))

    def set_published_chain(
        self,
        repo_id: int,
        bottom: str,
        chain_hash: str,
        branches: list[str],
        at: datetime,
    ) -> None:
        """Record ``branches`` as last published, and drop records whose bottom
        branch is no longer tracked (landed or removed)."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO published_chains "
                "(repo_id, bottom, chain_hash, branches, published_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (repo_id, bottom) DO UPDATE SET "
                "chain_hash = excluded.chain_hash, branches = excluded.branches, "
                "published_at = excluded.published_at",
                (repo_id, bottom, chain_hash, "\n".join(branches), at.isoformat()),
            )
            conn.execute(
                "DELETE FROM published_chains WHERE repo_id = ? AND bottom NOT IN "
                "(SELECT name FROM branches WHERE repo_id = ?)",
                (repo_id, repo_id),
            )

    def repos(self) -> list[RepoRecord]:
        rows = self._conn.execute(
            "SELECT id, name, path, git_common_dir, remote_url FROM repos "
            "ORDER BY name, path"
        )
        return [
            RepoRecord(
                id=r["id"],
                name=r["name"],
                path=Path(r["path"]),
                git_common_dir=Path(r["git_common_dir"]),
                remote_url=r["remote_url"],
            )
            for r in rows
        ]
