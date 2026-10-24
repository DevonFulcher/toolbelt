"""Which open tasks conflict with each other, found with real git in tmp."""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.ui import sibling_conflicts
from toolbelt.ui.live_status import EventHub, Fetched, TrackedBranch
from toolbelt.ui.sibling_conflicts import (
    SiblingConflictTracker,
    compute_repo,
    merge_conflicts,
    pairs_to_try,
    sorted_pair,
)

LINES = "".join(f"line {n}\n" for n in range(1, 41))


def _branch(repo: Path, name: str, parent: str, files: dict[str, str | bytes]) -> str:
    """Commits ``files`` on a new branch off ``parent``; returns its head."""
    git("checkout", "-q", "-B", name, parent, cwd=repo)
    for filename, content in files.items():
        path = repo / filename
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", name, cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    return head


@pytest.fixture
def base(repo: Path) -> Path:
    (repo / "shared.txt").write_text(LINES)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "shared", cwd=repo)
    return repo


def _replace(line: int, text: str) -> str:
    lines = LINES.splitlines(keepends=True)
    lines[line - 1] = f"{text}\n"
    return "".join(lines)


def _conflicts(repo: Path, a: str, b: str) -> tuple[str, ...]:
    heads = {n: git("rev-parse", n, cwd=repo) for n in (a, b)}
    pair = sorted_pair(heads[a], heads[b])
    return merge_conflicts(repo, [pair])[pair]


def test_branches_touching_different_files_merge_cleanly(base: Path):
    _branch(base, "a", "main", {"a.txt": "a\n"})
    _branch(base, "b", "main", {"b.txt": "b\n"})

    assert _conflicts(base, "a", "b") == ()


def test_the_same_line_edited_on_both_sides_conflicts_and_lists_the_file(base: Path):
    _branch(base, "a", "main", {"shared.txt": _replace(5, "from a")})
    _branch(base, "b", "main", {"shared.txt": _replace(5, "from b")})

    assert _conflicts(base, "a", "b") == ("shared.txt",)


def test_distant_hunks_of_one_file_merge_cleanly(base: Path):
    _branch(base, "a", "main", {"shared.txt": _replace(2, "from a")})
    _branch(base, "b", "main", {"shared.txt": _replace(30, "from b")})

    assert _conflicts(base, "a", "b") == ()


def test_a_rename_on_one_side_and_an_edit_on_the_other_merge_cleanly(base: Path):
    git("checkout", "-q", "-B", "a", "main", cwd=base)
    git("mv", "shared.txt", "renamed.txt", cwd=base)
    git("commit", "-q", "-m", "rename", cwd=base)
    git("checkout", "-q", "main", cwd=base)
    _branch(base, "b", "main", {"shared.txt": _replace(5, "from b")})

    assert _conflicts(base, "a", "b") == ()


def test_a_binary_file_changed_on_both_sides_conflicts(base: Path):
    _branch(base, "a", "main", {"blob.bin": b"\x00\x0a"})
    _branch(base, "b", "main", {"blob.bin": b"\x00\x0b"})

    assert _conflicts(base, "a", "b") == ("blob.bin",)


def test_a_bad_oid_raises_instead_of_reading_as_a_conflict(base: Path):
    good = git("rev-parse", "main", cwd=base)

    with pytest.raises(RuntimeError):
        merge_conflicts(base, [sorted_pair(good, "0" * 39 + "1")])


def test_pairs_in_one_chain_are_left_out():
    parents = {"a": "main", "b": "a", "c": "b", "d": "main"}

    assert pairs_to_try(parents, set(parents)) == [
        ("a", "d"),
        ("b", "d"),
        ("c", "d"),
    ]


class _Cache:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, ...]] = {}

    def load(self, repo_id: int):
        return dict(self.rows)

    def save(self, repo_id: int, pairs) -> None:
        self.rows.update(pairs)

    def prune(self, repo_id: int, heads: set[str]) -> None:
        self.rows = {
            pair: files
            for pair, files in self.rows.items()
            if pair[0] in heads and pair[1] in heads
        }


@pytest.fixture
def tried(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[str, str]]]:
    """The batches handed to git, in order."""
    batches: list[list[tuple[str, str]]] = []
    real = sibling_conflicts.merge_conflicts

    def recording(root: Path, pairs):
        batches.append(list(pairs))
        return real(root, pairs)

    monkeypatch.setattr(sibling_conflicts, "merge_conflicts", recording)
    return batches


def _tracker(
    repo: Path,
    parents: dict[str, str],
    *,
    fetched=lambda repo_id, branch: None,
    cache: _Cache | None = None,
):
    changes: list[set] = []
    tracker = SiblingConflictTracker(
        branches=lambda: [TrackedBranch(1, name, repo) for name in parents],
        parents=lambda repo_id: parents,
        fetched=fetched,
        cache=cache or _Cache(),
        on_change=changes.append,
        hub=EventHub(),
        branch_id=lambda repo_id, name: f"{repo_id}:{name}",
    )
    return tracker, changes


def _names(info) -> list[tuple[str, str]]:
    return [(c.name, c.state) for c in info.conflicts]


def test_the_tracker_reports_the_conflicting_task_with_its_files(base: Path):
    _branch(base, "a", "main", {"shared.txt": _replace(5, "from a")})
    _branch(base, "b", "main", {"shared.txt": _replace(5, "from b")})
    _branch(base, "c", "main", {"c.txt": "c\n"})
    tracker, changes = _tracker(base, {"a": "main", "b": "main", "c": "main"})

    asyncio.run(tracker.refresh(1))

    a = tracker.info(1, "a")
    assert a is not None
    assert [(c.name, c.state, c.files) for c in a.conflicts] == [
        ("b", "conflict", ["shared.txt"])
    ]
    assert a.head_known
    assert tracker.info(1, "c") is None
    assert changes == [{(1, "a"), (1, "b")}]


def test_a_pair_is_tried_once_for_the_same_commits(
    base: Path, tried: list[list[tuple[str, str]]]
):
    _branch(base, "a", "main", {"shared.txt": _replace(5, "from a")})
    _branch(base, "b", "main", {"shared.txt": _replace(5, "from b")})
    tracker, _ = _tracker(base, {"a": "main", "b": "main"})

    asyncio.run(tracker.refresh(1))
    asyncio.run(tracker.refresh(1))

    assert len(tried) == 1


def test_a_head_that_moves_recomputes_only_its_own_pairs(
    base: Path, tried: list[list[tuple[str, str]]]
):
    for name in "abc":
        _branch(base, name, "main", {f"{name}.txt": f"{name}\n"})
    tracker, changes = _tracker(base, {"a": "main", "b": "main", "c": "main"})
    asyncio.run(tracker.refresh(1))
    assert len(tried[0]) == 3

    moved = _branch(base, "a", "a", {"shared.txt": _replace(5, "from a")})
    _branch(base, "b", "b", {"shared.txt": _replace(5, "from b")})
    tried.clear()
    changes.clear()
    # b moved too: a's pairs with b and c, and b's with c (a-b counted once).
    asyncio.run(tracker.refresh(1))
    assert len(tried[0]) == 3
    tried.clear()

    _branch(base, "c", "c", {"c2.txt": "c2\n"})
    asyncio.run(tracker.refresh(1))

    assert {frozenset(pair) for pair in tried[0]} == {
        frozenset(sorted_pair(moved_head, git("rev-parse", "c", cwd=base)))
        for moved_head in (moved, git("rev-parse", "b", cwd=base))
    }


def test_stale_pairs_are_pruned_once_their_commits_are_no_longer_heads(
    base: Path,
):
    _branch(base, "a", "main", {"a.txt": "a\n"})
    _branch(base, "b", "main", {"b.txt": "b\n"})
    cache = _Cache()
    tracker, _ = _tracker(base, {"a": "main", "b": "main"}, cache=cache)
    asyncio.run(tracker.refresh(1))
    assert len(cache.rows) == 1

    _branch(base, "a", "a", {"a2.txt": "a2\n"})
    asyncio.run(tracker.refresh(1))

    assert len(cache.rows) == 1
    assert git("rev-parse", "a", cwd=base) in next(iter(cache.rows))


def test_cousins_conflicting_only_through_their_parents_are_inherited(
    base: Path,
):
    _branch(base, "p1", "main", {"shared.txt": _replace(5, "from p1")})
    _branch(base, "p2", "main", {"shared.txt": _replace(5, "from p2")})
    _branch(base, "c1", "p1", {"c1.txt": "c1\n"})
    _branch(base, "c2", "p2", {"c2.txt": "c2\n"})
    parents = {"p1": "main", "p2": "main", "c1": "p1", "c2": "p2"}
    tracker, _ = _tracker(base, parents)

    asyncio.run(tracker.refresh(1))

    c1 = tracker.info(1, "c1")
    assert c1 is not None
    assert _names(c1) == [("c2", "inherited"), ("p2", "conflict")]


def test_a_new_conflict_in_a_cousin_is_not_inherited(base: Path):
    _branch(base, "p1", "main", {"shared.txt": _replace(5, "from p1")})
    _branch(base, "p2", "main", {"shared.txt": _replace(5, "from p2")})
    _branch(base, "c1", "p1", {"blob.bin": b"\x00\x0a"})
    _branch(base, "c2", "p2", {"blob.bin": b"\x00\x0b"})
    parents = {"p1": "main", "p2": "main", "c1": "p1", "c2": "p2"}
    tracker, _ = _tracker(base, parents)

    asyncio.run(tracker.refresh(1))

    c1 = tracker.info(1, "c1")
    assert c1 is not None
    assert ("c2", "conflict") in _names(c1)


def test_a_pr_head_that_is_not_fetched_is_unknown_never_clean(base: Path):
    _branch(base, "a", "main", {"a.txt": "a\n"})

    def fetched(repo_id: int, branch: str):
        if branch != "remote":
            return None
        return Fetched(
            status=None,
            at=0.0,
            facts=SimpleNamespace(head_oid="1" * 40),  # type: ignore[arg-type]
        )

    tracker, _ = _tracker(base, {"a": "main", "remote": "main"}, fetched=fetched)

    asyncio.run(tracker.refresh(1))

    a = tracker.info(1, "a")
    assert a is not None and _names(a) == [("remote", "unknown")]
    remote = tracker.info(1, "remote")
    assert remote is not None and not remote.head_known
    assert _names(remote) == [("a", "unknown")]


def test_a_pr_head_that_is_local_is_used_when_the_branch_has_no_ref(base: Path):
    head = _branch(base, "tmp", "main", {"shared.txt": _replace(5, "from pr")})
    git("branch", "-D", "tmp", cwd=base)
    _branch(base, "a", "main", {"shared.txt": _replace(5, "from a")})

    def fetched(repo_id: int, branch: str):
        return (
            Fetched(
                status=None,
                at=0.0,
                facts=SimpleNamespace(head_oid=head),  # type: ignore[arg-type]
            )
            if branch == "pr"
            else None
        )

    tracker, _ = _tracker(base, {"a": "main", "pr": "main"}, fetched=fetched)
    asyncio.run(tracker.refresh(1))

    a = tracker.info(1, "a")
    assert a is not None and _names(a) == [("pr", "conflict")]


def test_only_a_branch_in_review_gets_the_prominent_flag(base: Path):
    _branch(base, "a", "main", {"shared.txt": _replace(5, "from a")})
    _branch(base, "b", "main", {"shared.txt": _replace(5, "from b")})

    def fetched(repo_id: int, branch: str):
        pr = PrState.OPEN if branch == "a" else PrState.DRAFT
        status = BranchStatus(
            pr=pr,
            ci=CiState.SUCCESS,
            review=ReviewState.NONE,
            has_conflicts=False,
            url=None,
            open_comments=0,
            bot_comments=0,
        )
        return Fetched(status=status, at=0.0, wall_at=datetime(2026, 1, 1))

    tracker, _ = _tracker(base, {"a": "main", "b": "main"}, fetched=fetched)
    asyncio.run(tracker.refresh(1))

    a, b = tracker.info(1, "a"), tracker.info(1, "b")
    assert a is not None and a.prominent
    assert b is not None and not b.prominent


def test_fresh_conflicts_sees_a_head_that_moved_since_the_last_pass(
    base: Path,
):
    _branch(base, "a", "main", {"a.txt": "a\n"})
    _branch(base, "b", "main", {"b.txt": "b\n"})
    tracker, _ = _tracker(base, {"a": "main", "b": "main"})
    asyncio.run(tracker.refresh(1))
    assert tracker.info(1, "a") is None

    _branch(base, "b", "b", {"shared.txt": _replace(5, "from b")})
    _branch(base, "a", "a", {"shared.txt": _replace(5, "from a")})

    fresh = asyncio.run(tracker.fresh_conflicts_for(1, "a"))

    assert fresh is not None and _names(fresh) == [("b", "conflict")]


def test_the_files_of_a_conflict_are_capped(base: Path):
    count = 13
    _branch(base, "a", "main", {f"f{n}.txt": "a\n" for n in range(count)})
    _branch(base, "b", "main", {f"f{n}.txt": "b\n" for n in range(count)})
    tracker, _ = _tracker(base, {"a": "main", "b": "main"})

    asyncio.run(tracker.refresh(1))

    a = tracker.info(1, "a")
    assert a is not None
    (conflict,) = a.conflicts
    assert len(conflict.files) == 10 and conflict.more == 3


def test_identical_heads_are_not_tried(base: Path):
    _branch(base, "a", "main", {"a.txt": "a\n"})
    git("branch", "b", "a", cwd=base)

    heads, found = compute_repo(
        base, 1, {"a": "main", "b": "main"}, {"a": "", "b": ""}, _Cache()
    )

    assert heads["a"] == heads["b"]
    assert found == {"a": [], "b": []}


def test_a_task_with_an_unknown_head_and_nothing_to_compare_with_says_nothing(
    base: Path,
):
    tracker, _ = _tracker(base, {"alone": "main"})

    asyncio.run(tracker.refresh(1))

    assert tracker.info(1, "alone") is None


def test_the_store_keeps_pairs_and_forgets_those_of_commits_that_are_not_heads(
    tmp_path: Path,
):
    from toolbelt.git.stack.store import StackStore

    store = StackStore(tmp_path / "stacks.db")
    repo_id = store.ensure_repo(
        SimpleNamespace(
            git_common_dir=tmp_path / ".git", path=tmp_path, remote_url=None
        )  # type: ignore[arg-type]
    )
    store.save_sibling_conflicts(
        repo_id, {("a", "b"): ("f.txt",), ("a", "c"): (), ("b", "c"): ("g",)}
    )

    store.prune_sibling_conflicts(repo_id, {"a", "b"})

    assert store.sibling_conflicts(repo_id) == {("a", "b"): ("f.txt",)}
