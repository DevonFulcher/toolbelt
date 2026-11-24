"""Unit tests for `repo_lock`'s cross-process serialization guarantee.

Threads are enough to exercise real contention here: `flock` locks are scoped
to the open file description, not the process, so two threads each opening
the lock file independently contend exactly like two separate processes would.
"""

import threading
import time
from pathlib import Path

import pytest
import typer

from toolbelt.git.repo_lock import named_mutex, repo_lock, stack_lock
from toolbelt.git.stack.append import create_stacked_branch


def _run_concurrently(roots: list[Path], *, hold_seconds: float = 0.05) -> int:
    """Enter `repo_lock(root)` on a thread per `roots` entry; return the
    largest number of holders observed inside the lock at once."""
    concurrent = 0
    max_concurrent = 0
    tally_lock = threading.Lock()

    def worker(root: Path) -> None:
        nonlocal concurrent, max_concurrent
        with repo_lock(root):
            with tally_lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            time.sleep(hold_seconds)
            with tally_lock:
                concurrent -= 1

    threads = [threading.Thread(target=worker, args=(root,)) for root in roots]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return max_concurrent


def test_serializes_concurrent_holders_in_the_same_worktree(repo: Path):
    assert _run_concurrently([repo] * 5) == 1


def test_serializes_across_different_worktrees_of_the_same_repo(
    repo: Path, tmp_path: Path
):
    # Lineage/refs/worktree registration are shared repo-wide, so a lock taken
    # from one worktree must also block a command running in another.
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)

    assert _run_concurrently([repo, api_wt, repo, api_wt]) == 1


def test_gives_up_with_a_clear_error_if_the_lock_never_frees(repo: Path):
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold_indefinitely() -> None:
        with repo_lock(repo):
            holder_ready.set()
            release_holder.wait(timeout=5)

    holder = threading.Thread(target=hold_indefinitely)
    holder.start()
    try:
        assert holder_ready.wait(timeout=5), "holder never acquired the lock"

        with pytest.raises(typer.Exit), repo_lock(repo, timeout=0.2):
            pass  # pragma: no cover - must time out before entering
    finally:
        release_holder.set()
        holder.join(timeout=5)


def _run_stack_locks(
    root: Path, keys: list[str], *, hold_seconds: float = 0.1
) -> tuple[int, int]:
    """Enter ``stack_lock`` on a thread per key. Returns the largest number of
    holders inside at once, and the largest number sharing one key."""
    inside: list[str] = []
    tally = threading.Lock()
    most_overall = 0
    most_per_key = 0

    def worker(key: str) -> None:
        nonlocal most_overall, most_per_key
        with stack_lock(root, stack_key=lambda: key):
            with tally:
                inside.append(key)
                most_overall = max(most_overall, len(inside))
                most_per_key = max(most_per_key, inside.count(key))
            time.sleep(hold_seconds)
            with tally:
                inside.remove(key)

    threads = [threading.Thread(target=worker, args=(key,)) for key in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return most_overall, most_per_key


def test_a_stack_lock_serializes_holders_of_the_same_stack(repo: Path):
    overall, per_key = _run_stack_locks(repo, ["devon/api"] * 4)

    assert (overall, per_key) == (1, 1)


def test_stack_locks_of_different_stacks_are_held_at_once(repo: Path):
    overall, per_key = _run_stack_locks(repo, ["devon/api", "devon/web", "devon/cli"])

    assert overall == 3
    assert per_key == 1


def test_the_repo_lock_excludes_every_stack_lock(repo: Path):
    stack_entered = threading.Event()
    holding_repo_lock = threading.Event()
    release_repo_lock = threading.Event()

    def hold_repo_lock() -> None:
        with repo_lock(repo):
            holding_repo_lock.set()
            release_repo_lock.wait(timeout=5)

    def take_stack_lock() -> None:
        with stack_lock(repo, stack_key=lambda: "devon/api"):
            stack_entered.set()

    holder = threading.Thread(target=hold_repo_lock)
    holder.start()
    assert holding_repo_lock.wait(timeout=5)
    waiter = threading.Thread(target=take_stack_lock)
    waiter.start()
    try:
        assert not stack_entered.wait(timeout=0.5)
    finally:
        release_repo_lock.set()
        holder.join(timeout=5)
        waiter.join(timeout=5)
    assert stack_entered.is_set()


def test_a_stack_lock_excludes_the_repo_lock(repo: Path):
    repo_entered = threading.Event()
    holding_stack_lock = threading.Event()
    release_stack_lock = threading.Event()

    def hold_stack_lock() -> None:
        with stack_lock(repo, stack_key=lambda: "devon/api"):
            holding_stack_lock.set()
            release_stack_lock.wait(timeout=5)

    def take_repo_lock() -> None:
        with repo_lock(repo):
            repo_entered.set()

    holder = threading.Thread(target=hold_stack_lock)
    holder.start()
    assert holding_stack_lock.wait(timeout=5)
    waiter = threading.Thread(target=take_repo_lock)
    waiter.start()
    try:
        assert not repo_entered.wait(timeout=0.5)
    finally:
        release_stack_lock.set()
        holder.join(timeout=5)
        waiter.join(timeout=5)
    assert repo_entered.is_set()


def test_locks_taken_inside_the_repo_lock_are_no_ops(repo: Path):
    with (
        repo_lock(repo),
        repo_lock(repo, timeout=0.2),
        stack_lock(repo, stack_key=lambda: "devon/api", timeout=0.2) as key,
    ):
        assert key == "devon/api"


def test_the_repo_lock_cannot_be_taken_inside_a_stack_lock(repo: Path):
    with (
        stack_lock(repo, stack_key=lambda: "devon/api"),
        pytest.raises(RuntimeError, match="stack_lock"),
        repo_lock(repo),
    ):
        pass  # pragma: no cover


def test_a_second_stack_cannot_be_locked_inside_a_stack_lock(repo: Path):
    with (
        stack_lock(repo, stack_key=lambda: "devon/api"),
        pytest.raises(RuntimeError, match="already holds"),
        stack_lock(repo, stack_key=lambda: "devon/web"),
    ):
        pass  # pragma: no cover


def test_a_stack_whose_key_keeps_changing_falls_back_to_the_repo_lock(repo: Path):
    changes = iter(range(100))

    with stack_lock(repo, stack_key=lambda: f"devon/moved-{next(changes)}") as key:
        # Held exclusively now: a second repo-wide lock request on another
        # thread cannot enter until this block ends.
        other_entered = threading.Event()

        def take_repo_lock() -> None:
            with repo_lock(repo):
                other_entered.set()

        other = threading.Thread(target=take_repo_lock)
        other.start()
        assert not other_entered.wait(timeout=0.5)
    other.join(timeout=5)
    assert other_entered.is_set()
    assert key.startswith("devon/moved-")


def test_a_stack_key_that_changes_once_is_retried(repo: Path):
    keys = iter(["devon/old", "devon/new", "devon/new", "devon/new"])

    with stack_lock(repo, stack_key=lambda: next(keys)) as key:
        assert key == "devon/new"


def test_a_named_mutex_serializes_holders_and_is_not_reentrant(repo: Path):
    inside = 0
    most = 0
    tally = threading.Lock()

    def worker() -> None:
        nonlocal inside, most
        with named_mutex(repo, "fetch"):
            with tally:
                inside += 1
                most = max(most, inside)
            time.sleep(0.05)
            with tally:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most == 1

    with (
        named_mutex(repo, "fetch"),
        pytest.raises(RuntimeError, match="fetch"),
        named_mutex(repo, "fetch"),
    ):
        pass  # pragma: no cover
