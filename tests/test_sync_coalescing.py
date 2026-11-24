"""Coalescing of redundant syncs and fetches, with real git in tmp.

A sync that began after a caller asked for one, and finished while that caller
waited for the stack lock, stands in for the caller's own. These tests hold the
lock to stage that wait deterministically.
"""

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import typer
from conftest import FakeForge, git

from toolbelt.git.repo_lock import stack_lock, stack_scope
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.store import SyncRun
from toolbelt.git.sync_coalescing import SyncLedger, fetch_once
from toolbelt.git.workflow import sync_stack_coalesced

_API_SCOPE = stack_scope("devon/api")


def _build_stack(repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """main <- devon/api <- devon/api_tests, each in its own worktree."""
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    tests_wt = tmp_path / "wt-api-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    return api_wt, tests_wt


def _commit_on_api(api_wt: Path) -> None:
    (api_wt / "api.txt").write_text("api change\n")
    git("add", "-A", cwd=api_wt)
    git("commit", "-m", "api work", cwd=api_wt)


def _sync(tests_wt: Path, *, merge_base_branch: bool = False) -> None:
    sync_stack_coalesced(
        root=tests_wt,
        forge=FakeForge(),
        branch=None,
        merge_base_branch=merge_base_branch,
    )


def _sync_while_waiting_on(
    repo: Path, tests_wt: Path, *, during_wait: Callable[[SyncLedger], None]
) -> None:
    """Hold the stack lock, start a sync that has to wait for it, run
    ``during_wait`` once that sync has taken its ticket, then release."""
    ledger = SyncLedger(repo)
    errors: list[BaseException] = []

    def follower() -> None:
        try:
            _sync(tests_wt)
        except BaseException as err:  # noqa: BLE001 - re-raised on the main thread
            errors.append(err)

    with stack_lock(repo, stack_key=lambda: "devon/api"):
        before = ledger.current_ticket()
        thread = threading.Thread(target=follower)
        thread.start()
        deadline = time.monotonic() + 10
        while ledger.current_ticket() == before:
            assert time.monotonic() < deadline, "follower never took a ticket"
            time.sleep(0.01)
        during_wait(ledger)
    thread.join(timeout=30)
    assert not thread.is_alive()
    if errors:
        raise errors[0]


def test_tickets_only_grow_and_a_run_covers_the_tickets_taken_before_it(
    repo: Path,
):
    ledger = SyncLedger(repo)
    earlier = ledger.take_ticket()
    also_earlier = ledger.take_ticket()
    started_at = ledger.current_ticket()
    later = ledger.take_ticket()

    assert earlier < also_earlier < later
    assert not ledger.covers("some-scope", ticket=earlier)

    ledger.record("some-scope", SyncRun(started_at, merge_base_branch=False))

    assert ledger.covers("some-scope", ticket=earlier)
    assert ledger.covers("some-scope", ticket=also_earlier)
    assert not ledger.covers("some-scope", ticket=later)
    assert not ledger.covers("another-scope", ticket=earlier)


def test_a_run_without_the_base_merge_does_not_cover_a_call_that_wants_it(
    repo: Path,
):
    ledger = SyncLedger(repo)
    ticket = ledger.take_ticket()
    ledger.record(
        "some-scope", SyncRun(ledger.current_ticket(), merge_base_branch=False)
    )

    assert ledger.covers("some-scope", ticket=ticket, merge_base_branch=False)
    assert not ledger.covers("some-scope", ticket=ticket, merge_base_branch=True)

    ledger.record(
        "some-scope", SyncRun(ledger.current_ticket(), merge_base_branch=True)
    )

    assert ledger.covers("some-scope", ticket=ticket, merge_base_branch=True)
    assert ledger.covers("some-scope", ticket=ticket, merge_base_branch=False)


def test_a_sync_runs_when_no_sync_began_after_it_was_requested(
    repo: Path, tmp_path: Path
):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    _commit_on_api(api_wt)

    def an_older_run_finishes(ledger: SyncLedger) -> None:
        # Began before the waiting sync took its ticket: covers nothing.
        ledger.record(_API_SCOPE, SyncRun(started_at=0, merge_base_branch=False))

    _sync_while_waiting_on(repo, tests_wt, during_wait=an_older_run_finishes)

    assert (tests_wt / "api.txt").read_text() == "api change\n"


def test_a_sync_is_skipped_when_one_began_after_it_was_requested(
    repo: Path, tmp_path: Path
):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    _commit_on_api(api_wt)

    def a_newer_run_finishes(ledger: SyncLedger) -> None:
        # The run is only recorded here, not performed, so a skipped sync
        # leaves the commit unpropagated.
        ledger.record(
            _API_SCOPE,
            SyncRun(started_at=ledger.current_ticket(), merge_base_branch=False),
        )

    _sync_while_waiting_on(repo, tests_wt, during_wait=a_newer_run_finishes)

    assert not (tests_wt / "api.txt").exists()


def test_a_sync_that_wants_the_base_merge_is_not_skipped_by_one_without_it(
    repo: Path, tmp_path: Path
):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (api_wt / "README.md").write_text("stack version\n")
    git("add", "README.md", cwd=api_wt)
    git("commit", "-m", "stack changes README", cwd=api_wt)
    (repo / "README.md").write_text("main version\n")
    git("add", "README.md", cwd=repo)
    git("commit", "-m", "main changes README", cwd=repo)
    git("push", "origin", "main", cwd=repo)
    ledger = SyncLedger(repo)
    ledger.record(_API_SCOPE, SyncRun(started_at=10**9, merge_base_branch=False))

    # Merging the base conflicts with the README change: it ran, rather than
    # being skipped as already covered.
    with pytest.raises(typer.Exit):
        _sync(tests_wt, merge_base_branch=True)

    assert "UU README.md" in git("status", "--porcelain", cwd=api_wt)


def test_a_sync_that_hit_a_conflict_is_not_recorded_so_the_next_one_hits_it_too(
    repo: Path, tmp_path: Path
):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    (api_wt / "README.md").write_text("stack version\n")
    git("add", "README.md", cwd=api_wt)
    git("commit", "-m", "stack changes README", cwd=api_wt)
    (repo / "README.md").write_text("main version\n")
    git("add", "README.md", cwd=repo)
    git("commit", "-m", "main changes README", cwd=repo)
    git("push", "origin", "main", cwd=repo)

    with pytest.raises(typer.Exit):
        _sync(tests_wt)
    with pytest.raises(typer.Exit):
        _sync(tests_wt)

    assert not SyncLedger(repo).covers(_API_SCOPE, ticket=1)


def test_concurrent_syncs_of_one_stack_all_succeed_and_converge(
    repo: Path, tmp_path: Path
):
    api_wt, tests_wt = _build_stack(repo, tmp_path)
    _commit_on_api(api_wt)
    errors: list[BaseException] = []

    def sync() -> None:
        try:
            _sync(tests_wt)
        except BaseException as err:  # noqa: BLE001 - re-raised below
            errors.append(err)

    threads = [threading.Thread(target=sync) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert errors == []
    assert (tests_wt / "api.txt").read_text() == "api change\n"
    assert git("rev-parse", "devon/api_tests", cwd=repo) == git(
        "rev-parse", "origin/devon/api_tests", cwd=repo
    )


def test_concurrent_syncs_of_different_stacks_both_converge(repo: Path, tmp_path: Path):
    api_wt, api_tests_wt = _build_stack(repo, tmp_path)
    web_wt = tmp_path / "wt-web"
    create_stacked_branch("web", root=repo, wt_path=web_wt)
    web_tests_wt = tmp_path / "wt-web-tests"
    create_stacked_branch("web_tests", root=web_wt, wt_path=web_tests_wt)
    _commit_on_api(api_wt)
    (web_wt / "web.txt").write_text("web change\n")
    git("add", "-A", cwd=web_wt)
    git("commit", "-m", "web work", cwd=web_wt)
    errors: list[BaseException] = []

    def sync(wt: Path) -> None:
        try:
            _sync(wt)
        except BaseException as err:  # noqa: BLE001 - re-raised below
            errors.append(err)

    threads = [
        threading.Thread(target=sync, args=(wt,))
        for wt in (api_tests_wt, web_tests_wt, api_tests_wt, web_tests_wt)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert errors == []
    assert (api_tests_wt / "api.txt").read_text() == "api change\n"
    assert (web_tests_wt / "web.txt").read_text() == "web change\n"
    assert not (api_tests_wt / "web.txt").exists()
    for branch in ("devon/api_tests", "devon/web_tests"):
        assert git("rev-parse", branch, cwd=repo) == git(
            "rev-parse", f"origin/{branch}", cwd=repo
        )


def test_every_fetch_runs_when_none_overlap(repo: Path):
    fetches: list[int] = []

    fetch_once(repo, fetch=lambda: fetches.append(1))
    fetch_once(repo, fetch=lambda: fetches.append(2))

    assert fetches == [1, 2]


def test_a_recorded_fetch_covers_earlier_tickets_but_not_later_calls(repo: Path):
    ledger = SyncLedger(repo)
    fetches: list[int] = []
    waiting_ticket = ledger.take_ticket()
    ledger.record(
        "fetch",
        SyncRun(started_at=ledger.current_ticket(), merge_base_branch=False),
    )

    # A call whose own ticket comes after that fetch started must still fetch.
    fetch_once(repo, fetch=lambda: fetches.append(1))

    assert ledger.covers("fetch", ticket=waiting_ticket)
    assert fetches == [1]


def test_concurrent_fetches_never_overlap_and_each_call_returns(repo: Path):
    running = 0
    most = 0
    tally = threading.Lock()
    calls = 0

    def fetch() -> None:
        nonlocal running, most, calls
        with tally:
            running += 1
            calls += 1
            most = max(most, running)
        time.sleep(0.05)
        with tally:
            running -= 1

    threads = [
        threading.Thread(target=fetch_once, args=(repo,), kwargs={"fetch": fetch})
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert most == 1
    assert 1 <= calls <= 8
