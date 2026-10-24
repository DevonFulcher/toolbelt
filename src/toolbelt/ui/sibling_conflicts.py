"""Which open tasks of a repo would conflict if merged with each other.

Every pair of tracked branches of a repo whose PRs are not merged or closed,
except pairs in one chain of the stack (a branch and any ancestor of it: that is
``tt g sync``'s job), is tried with ``git merge-tree --write-tree``, which
merges the two heads in memory and touches no checkout. The answer is a pure
function of the two commits (their natural merge base included), so it is kept
for good by (repo, oid, oid) in the ``sibling_conflicts`` table and dropped when
either commit stops being a head. Only a head that moved has pairs to try: all
the others come from the table. One ``git merge-tree --stdin`` runs per repo
and pass.

* What conflicts is what git's text merge conflicts on: both sides changing
  the same or adjoining lines (the file is listed), both changing a binary file
  or a lockfile. Distant hunks and renames merge cleanly. Nothing ignores a
  path. Semantic breakage (a renamed function still called by the other side)
  cannot be seen.
* Three-way interactions (A, B and C each merge with the others, but not all
  three together) are not tried. For text merges a conflict needs two
  overlapping or adjoining changes, and those are visible in a pair; a triple
  can only conflict through rename and directory-rename edge cases.
* Two cousins can conflict only because their parents do. A pair whose
  conflicted files are all files its parents conflict in is ``inherited``.
* Heads come from the local branch ref, else from the PR's head commit. This
  never fetches: a head that is not in the local repo is ``unknown`` (never
  shown as clean).
* The ancestry of the tracked stack is only used to leave out pairs in one
  chain. A conflict is never decided from ancestry, because squash merges make
  it unreliable: it comes only from merging the trees.

``SiblingConflictTracker`` re-reads the heads every ``tick`` seconds while a
browser watches (a local commit moves a head without GitHub knowing), and again
on ``fresh_conflicts_for``, which a caller about to start a review or merge
awaits for an up to date answer.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from toolbelt import logged_process
from toolbelt.git.stack.status import PrState
from toolbelt.git.stack.store import Parents
from toolbelt.logger import logger
from toolbelt.ui import api_models as api
from toolbelt.ui.conflict_commit import MAX_FILES
from toolbelt.ui.live_status import EventHub, Fetched, TrackedBranch

type Pair = tuple[str, str]
type BranchKey = tuple[int, str]

_OID_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

#: How often the heads are read again (seconds).
DEFAULT_HEADS_TICK_SECONDS = 10.0


@dataclass(frozen=True)
class PairConflict:
    """A branch's merge with ``other`` conflicts (or could not be tried)."""

    other: str
    state: api.SiblingConflictState
    files: tuple[str, ...]


class ConflictCache(Protocol):
    """Where the pairs already tried are kept (the ``sibling_conflicts``
    table). Called from worker threads."""

    def load(self, repo_id: int) -> dict[Pair, tuple[str, ...]]: ...

    def save(self, repo_id: int, pairs: dict[Pair, tuple[str, ...]]) -> None: ...

    def prune(self, repo_id: int, heads: set[str]) -> None: ...


def _git(root: Path, *args: str, stdin: str | None = None):
    return logged_process.run(
        ["git", *args],
        cwd=root,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def sorted_pair(a: str, b: str) -> Pair:
    return (a, b) if a <= b else (b, a)


def merge_conflicts(root: Path, pairs: list[Pair]) -> dict[Pair, tuple[str, ...]]:
    """The conflicted files of merging each pair of commits (empty when it
    merges cleanly), tried by one ``git merge-tree --stdin``. Every oid must be
    a commit in the repo: git stops the whole batch on one that is not, and
    that is raised, never read as a conflict.

    With ``--stdin`` git writes, for each line and NUL separated, ``1`` (clean)
    or ``0`` (conflicts), the merged tree and, with ``--name-only``, the
    conflicted files followed by an empty field."""
    if not pairs:
        return {}
    result = _git(
        root,
        "merge-tree",
        "--stdin",
        "--write-tree",
        "--name-only",
        "--no-messages",
        stdin="".join(f"{a} {b}\n" for a, b in pairs),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git merge-tree failed")
    fields = result.stdout.split("\0")
    found: dict[Pair, tuple[str, ...]] = {}
    at = 0
    for pair in pairs:
        status, tree = fields[at], fields[at + 1]
        if status not in ("0", "1") or not _OID_RE.fullmatch(tree):
            raise RuntimeError(f"unexpected git merge-tree output for {pair}")
        at += 2
        files = []
        while fields[at]:
            files.append(fields[at])
            at += 1
        at += 1
        if (status == "0") != bool(files):
            raise RuntimeError(f"unexpected git merge-tree output for {pair}")
        found[pair] = tuple(files)
    return found


def local_heads(root: Path) -> dict[str, str]:
    """Every local branch's commit."""
    result = _git(
        root, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads"
    )
    heads = {}
    for line in result.stdout.splitlines():
        oid, _, ref = line.partition(" ")
        heads[ref.removeprefix("refs/heads/")] = oid
    return heads


def existing_commits(root: Path, oids: set[str]) -> set[str]:
    """The ``oids`` that are commits in the repo."""
    if not oids:
        return set()
    result = _git(
        root, "cat-file", "--batch-check", stdin="".join(f"{o}\n" for o in oids)
    )
    present = set()
    for line in result.stdout.splitlines():
        oid, _, rest = line.partition(" ")
        if rest.startswith("commit "):
            present.add(oid)
    return present


def _ancestors(parents: Parents, branch: str) -> set[str]:
    found: set[str] = set()
    parent = parents.get(branch)
    while parent is not None and parent not in found:
        found.add(parent)
        parent = parents.get(parent)
    return found


def pairs_to_try(parents: Parents, branches: set[str]) -> list[tuple[str, str]]:
    """Every pair of ``branches`` that are not in one chain of the stack."""
    ordered = sorted(branches)
    return [
        (a, b)
        for index, a in enumerate(ordered)
        for b in ordered[index + 1 :]
        if a not in _ancestors(parents, b) and b not in _ancestors(parents, a)
    ]


def conflicts_of(
    parents: Parents,
    heads: dict[str, str | None],
    tried: dict[Pair, tuple[str, ...]],
) -> dict[str, list[PairConflict]]:
    """For each branch in ``heads`` (``None``: head not local), the branches
    it conflicts with or could not be compared with, from the pairs ``tried``."""
    found: dict[str, list[PairConflict]] = {name: [] for name in heads}
    for a, b in pairs_to_try(parents, set(heads)):
        oid_a, oid_b = heads[a], heads[b]
        if oid_a is None or oid_b is None:
            # A missing head cannot be compared: each side is told it could
            # not be tried against the other, never that it is clean.
            found[a].append(PairConflict(b, "unknown", ()))
            found[b].append(PairConflict(a, "unknown", ()))
            continue
        files = tried.get(sorted_pair(oid_a, oid_b), ())
        if not files:
            continue
        state: api.SiblingConflictState = "conflict"
        parent_a, parent_b = parents.get(a), parents.get(b)
        if (
            parent_a in heads
            and parent_b in heads
            and parent_a != parent_b
            and (head_a := heads[parent_a]) is not None
            and (head_b := heads[parent_b]) is not None
            and set(files) <= set(tried.get(sorted_pair(head_a, head_b), ()))
        ):
            state = "inherited"
        found[a].append(PairConflict(b, state, files))
        found[b].append(PairConflict(a, state, files))
    return found


def compute_repo(
    root: Path,
    repo_id: int,
    parents: Parents,
    pr_heads: dict[str, str],
    cache: ConflictCache,
) -> tuple[dict[str, str | None], dict[str, list[PairConflict]]]:
    """Reads the heads of the branches in ``pr_heads`` (each one's PR head,
    ``""`` when it has no PR lookup; a local branch ref wins) and tries the
    pairs the cache does not have. Returns the heads (``None``: not in the
    local repo) and each branch's conflicts."""
    local = local_heads(root)
    wanted = {name: local.get(name) or pr_head for name, pr_head in pr_heads.items()}
    present = existing_commits(root, {oid for oid in wanted.values() if oid})
    heads = {name: oid if oid in present else None for name, oid in wanted.items()}
    tried = cache.load(repo_id)
    missing = sorted(
        {
            sorted_pair(oid_a, oid_b)
            for a, b in pairs_to_try(parents, set(heads))
            if (oid_a := heads[a]) and (oid_b := heads[b]) and oid_a != oid_b
        }
        - set(tried)
    )
    if missing:
        new = merge_conflicts(root, missing)
        cache.save(repo_id, new)
        tried.update(new)
    cache.prune(repo_id, {oid for oid in heads.values() if oid})
    return heads, conflicts_of(parents, heads, tried)


class SiblingConflictLookup(Protocol):
    """What the snapshot reads (``SiblingConflictTracker`` is one)."""

    def info(self, repo_id: int, branch: str) -> api.SiblingConflictsInfo | None: ...


class _NoSiblingConflicts:
    def info(self, repo_id: int, branch: str) -> api.SiblingConflictsInfo | None:
        return None


#: Stands in where nothing is tried (as in tests that build a snapshot).
NO_SIBLING_CONFLICTS: SiblingConflictLookup = _NoSiblingConflicts()


@dataclass(frozen=True)
class _RepoResult:
    heads: dict[str, str | None]
    conflicts: dict[str, list[PairConflict]]


class SiblingConflictTracker:
    def __init__(
        self,
        *,
        branches: Callable[[], list[TrackedBranch]],
        parents: Callable[[int], Parents],
        fetched: Callable[[int, str], Fetched | None],
        cache: ConflictCache,
        on_change: Callable[[set[BranchKey]], None],
        hub: EventHub,
        branch_id: Callable[[int, str], str],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = DEFAULT_HEADS_TICK_SECONDS,
    ) -> None:
        self._branches = branches
        self._parents = parents
        self._fetched = fetched
        self._cache = cache
        self._on_change = on_change
        self._hub = hub
        self._branch_id = branch_id
        self._sleep = sleep
        self._tick = tick
        self._results: dict[int, _RepoResult] = {}
        #: One pass at a time per repo.
        self._locks: dict[int, asyncio.Lock] = {}

    # ---- what the cards show -------------------------------------------------

    def info(self, repo_id: int, branch: str) -> api.SiblingConflictsInfo | None:
        """``None`` when the branch merges cleanly with the others, or nothing
        is worked out for it yet."""
        result = self._results.get(repo_id)
        if result is None or branch not in result.heads:
            return None
        head_known = result.heads[branch] is not None
        conflicts = result.conflicts.get(branch, [])
        if not conflicts:
            # Nothing to say: it merges cleanly with every other open task, or
            # has none to try (an unknown head alone is not worth a chip).
            return None
        looked_up = self._fetched(repo_id, branch)
        return api.SiblingConflictsInfo(
            head_known=head_known,
            prominent=looked_up is not None
            and looked_up.status is not None
            and looked_up.status.pr is PrState.OPEN,
            conflicts=[
                api.SiblingConflict(
                    branch_id=self._branch_id(repo_id, c.other),
                    name=c.other,
                    state=c.state,
                    files=list(c.files[:MAX_FILES]),
                    more=max(len(c.files) - MAX_FILES, 0),
                )
                for c in sorted(conflicts, key=lambda c: c.other)
            ],
        )

    async def fresh_conflicts_for(
        self, repo_id: int, branch: str
    ) -> api.SiblingConflictsInfo | None:
        """Reads the heads again and works out the repo's pairs now, then
        answers for ``branch`` (``None``: it merges cleanly with the other open
        tasks). For a caller that must not act on a result that is a poll old:
        right before a personal review starts and before a merge. Pairs already
        tried are reused: a result depends only on the two commits."""
        await self.refresh(repo_id)
        return self.info(repo_id, branch)

    # ---- working them out ----------------------------------------------------

    def _open_branches(self, repo_id: int) -> tuple[Path, dict[str, str]] | None:
        """The repo's checkout and each tracked branch whose PR is not merged
        or closed, with its PR's head (``""``: no lookup yet or no PR)."""
        found: dict[str, str] = {}
        path: Path | None = None
        for tracked in self._branches():
            if tracked.repo_id != repo_id:
                continue
            path = tracked.path
            looked_up = self._fetched(repo_id, tracked.branch)
            if (
                looked_up is not None
                and looked_up.status is not None
                and looked_up.status.pr in (PrState.MERGED, PrState.CLOSED)
            ):
                continue
            found[tracked.branch] = (
                looked_up.facts.head_oid
                if looked_up is not None and looked_up.facts is not None
                else ""
            )
        return (path, found) if path is not None else None

    async def refresh(self, repo_id: int) -> set[BranchKey]:
        """Works out the repo's pairs; returns the branches whose result
        changed (and reports them through ``on_change``)."""
        lock = self._locks.setdefault(repo_id, asyncio.Lock())
        async with lock:
            opened = self._open_branches(repo_id)
            if opened is None:
                self._results.pop(repo_id, None)
                return set()
            path, pr_heads = opened
            heads, conflicts = await asyncio.to_thread(
                compute_repo,
                path,
                repo_id,
                self._parents(repo_id),
                pr_heads,
                self._cache,
            )
            previous = self._results.get(repo_id)
            self._results[repo_id] = _RepoResult(heads, conflicts)
        changed = (
            {
                (repo_id, name)
                for name in heads
                if (previous.heads.get(name), previous.conflicts.get(name, []))
                != (heads[name], conflicts[name])
            }
            if previous is not None
            else {
                # A first pass announces only the branches that have something to
                # show: the snapshot has nothing for the others.
                (repo_id, name)
                for name in heads
                if conflicts[name] or heads[name] is None
            }
        )
        if changed:
            self._on_change(changed)
        return changed

    async def run(self) -> None:
        while True:
            await self._hub.wait_for_subscriber()
            while self._hub.watched():
                for repo_id in sorted({b.repo_id for b in self._branches()}):
                    try:
                        await self.refresh(repo_id)
                    except Exception:  # noqa: BLE001
                        # A background pass: a repo whose git fails is tried
                        # again at the next tick and must not stop the others.
                        logger.exception(
                            "could not try the open tasks of repo %s against "
                            "each other",
                            repo_id,
                        )
                await self._sleep(self._tick)
