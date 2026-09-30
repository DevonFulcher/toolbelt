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

from toolbelt.git.repo_lock import repo_lock
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

        with pytest.raises(typer.Exit):
            with repo_lock(repo, timeout=0.2):
                pass  # pragma: no cover - must time out before entering
    finally:
        release_holder.set()
        holder.join(timeout=5)
