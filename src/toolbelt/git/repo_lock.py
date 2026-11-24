"""Serialize toolbelt's git-mutating commands across processes.

Multiple agents/shells can run toolbelt commands against the same repo at
once — including from different worktrees, since lineage config, branches,
and worktree registration are all shared across a repo's worktrees (they live
in the common ``.git`` dir, not per-worktree). Interleaving two multi-step
commands (``sync``, ``branch-clean``, ``remove``, ...) can leave the stack
lineage inconsistent: a branch reparented by one process while another is
mid-restack onto it, two pushes racing the same branch, and so on.

Every lock here is an OS-level advisory lock (``flock``) on a file inside the
repo's shared ``.git`` directory, so every worktree of the same repo contends
for the exact same lock, and a second command just waits for the first to
finish rather than interleaving with it. Each wait is bounded: a lock that
isn't free within ``timeout`` seconds means whatever holds it is stuck (or the
machine it was on died without a clean shutdown), not just slow — a timeout
can't undo whatever that stuck process left half-done (a lock has no idea what
its holder was doing), but it stops every other command piling up behind it
forever with no signal anything is wrong.

There are three kinds of lock:

- ``repo_lock``: exclusive over the whole repo. Every command that may touch
  more than one stack (``append``, ``remove``, ``compress``, ``branch-clean``,
  ``set-parent``, ...) takes it, as before.
- ``stack_lock``: shared over the repo plus exclusive over one stack (the
  subtree under a top-of-stack branch). A command that only reads and writes
  that stack's branches and worktrees takes it, so syncs of different stacks
  run side by side while ``repo_lock`` holders still wait for all of them and
  exclude them.
- ``named_mutex``: a short exclusive lock around one piece of repo-wide state
  that stack-locked commands share (the ticket counter, a ``git fetch``, ``git
  config`` writes, worktree removal). It never nests with itself and is held
  only around that one operation.

Locks are not reentrant at the OS level (a second attempt on the same file
from the same process would block on itself forever), so they are tracked per
thread: a ``repo_lock`` taken while this thread already holds it exclusively,
or a ``stack_lock`` taken while it holds ``repo_lock``, is a no-op (the outer
lock already excludes everyone the inner one would). Asking for a stronger lock
than the one held (``repo_lock`` inside a ``stack_lock``, a second stack inside
a stack) would deadlock against another process doing the reverse, so it
raises instead.
"""

import contextlib
import fcntl
import hashlib
import math
import threading
import time
from collections.abc import Callable, Iterator
from enum import Enum
from pathlib import Path
from typing import assert_never

import typer

from toolbelt.git.exec import capture
from toolbelt.logger import logger

_REPO_LOCK_FILENAME = "toolbelt.lock"
_DEFAULT_TIMEOUT_SECONDS = 300.0
_POLL_INTERVAL_SECONDS = 0.2
#: How often a stack's top-of-stack branch may change between resolving it and
#: holding its lock (a landing reparents the stack's children) before the
#: command falls back to the repo-wide lock.
_STACK_KEY_ATTEMPTS = 3


class _Hold(Enum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class _ThreadHolds(threading.local):
    """What the current thread holds, so nested requests don't self-deadlock."""

    def __init__(self) -> None:
        self.repo: dict[Path, _Hold] = {}
        self.mutexes: set[Path] = set()


_holds = _ThreadHolds()


def git_common_dir(root: Path) -> Path:
    """The repo's shared ``.git`` directory, the same from every worktree."""
    common_dir = Path(capture(["git", "rev-parse", "--git-common-dir"], cwd=root))
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    return common_dir


def stack_scope(stack_key: str) -> str:
    """A filename-safe identifier for the stack whose top branch is ``stack_key``."""
    return hashlib.sha256(stack_key.encode()).hexdigest()[:16]


@contextlib.contextmanager
def _flock(lock_path: Path, *, operation: int, timeout: float) -> Iterator[None]:
    """Hold ``operation`` (``LOCK_SH`` or ``LOCK_EX``) on ``lock_path`` for the
    ``with`` block, waiting (after logging once) up to ``timeout`` seconds."""
    with open(lock_path, "w") as lock_file:
        try:
            fcntl.flock(lock_file, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Waiting for another toolbelt command to finish...")
            polls = math.ceil(timeout / _POLL_INTERVAL_SECONDS)
            for _ in range(polls):
                time.sleep(_POLL_INTERVAL_SECONDS)
                try:
                    fcntl.flock(lock_file, operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    continue
            else:
                logger.error(
                    f"Timed out after {timeout:.0f}s waiting for "
                    f"another toolbelt command to finish ({lock_path}"
                    "). A crashed or killed process can't leave this "
                    "stuck (the OS releases it when the process "
                    "dies), so whatever holds it is still running "
                    "but wedged — find and check that process (a "
                    "hung `gh`/`git` call, or an unanswered prompt)."
                )
                raise typer.Exit(1) from None
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


@contextlib.contextmanager
def _hold_repo(common_dir: Path, *, hold: _Hold, timeout: float) -> Iterator[None]:
    repo_path = common_dir / _REPO_LOCK_FILENAME
    match hold:
        case _Hold.EXCLUSIVE:
            operation = fcntl.LOCK_EX
        case _Hold.SHARED:
            operation = fcntl.LOCK_SH
        case _:
            assert_never(hold)
    with _flock(repo_path, operation=operation, timeout=timeout):
        _holds.repo[repo_path] = hold
        try:
            yield
        finally:
            del _holds.repo[repo_path]


@contextlib.contextmanager
def repo_lock(
    root: Path, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Hold an exclusive lock on the repo ``root`` belongs to, for the
    ``with`` block's duration. Blocks (after logging once) if another
    toolbelt command already holds any lock on it, anywhere in the repo — up
    to ``timeout`` seconds, after which this exits with an error rather than
    waiting forever. A no-op when this thread already holds it exclusively.
    """
    common_dir = git_common_dir(root)
    held = _holds.repo.get(common_dir / _REPO_LOCK_FILENAME)
    match held:
        case _Hold.EXCLUSIVE:
            yield
        case _Hold.SHARED:
            raise RuntimeError(
                "repo_lock requested while this thread holds a stack_lock; "
                "release the stack lock first (upgrading would deadlock)."
            )
        case None:
            with _hold_repo(common_dir, hold=_Hold.EXCLUSIVE, timeout=timeout):
                yield


@contextlib.contextmanager
def stack_lock(
    root: Path,
    *,
    stack_key: Callable[[], str],
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[str]:
    """Hold the lock of one stack, plus a shared hold on the repo, and yield
    the stack's key.

    ``stack_key`` names the stack by its top-of-stack branch, read from the
    lineage. The lineage can move between reading it and holding the lock (a
    landing reparents the stack's children, so they become tops of their own
    stacks), so the key is read again once the lock is held and the attempt
    restarts if it changed; after ``_STACK_KEY_ATTEMPTS`` restarts this falls
    back to ``repo_lock``, which excludes every stack.

    A no-op (yielding the key) when this thread holds ``repo_lock``.
    """
    common_dir = git_common_dir(root)
    repo_path = common_dir / _REPO_LOCK_FILENAME
    match _holds.repo.get(repo_path):
        case _Hold.EXCLUSIVE:
            yield stack_key()
            return
        case _Hold.SHARED:
            raise RuntimeError(
                "stack_lock requested while this thread already holds one; "
                "two stacks at once could deadlock against a process taking "
                "them in the other order."
            )
        case None:
            pass

    for _ in range(_STACK_KEY_ATTEMPTS):
        key = stack_key()
        stack_path = common_dir / f"toolbelt-stack-{stack_scope(key)}.lock"
        with (
            _hold_repo(common_dir, hold=_Hold.SHARED, timeout=timeout),
            _flock(stack_path, operation=fcntl.LOCK_EX, timeout=timeout),
        ):
            if stack_key() == key:
                yield key
                return
    with repo_lock(root, timeout=timeout):
        yield stack_key()


@contextlib.contextmanager
def named_mutex(
    root: Path, name: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Hold the repo-wide exclusive mutex ``name`` around one short operation.

    Never skipped, even under ``repo_lock``: the holders it protects against
    include commands that hold no repo lock at all (taking a ticket happens
    before a stack lock is requested). Not reentrant, and must not wrap
    another lock request.
    """
    mutex_path = git_common_dir(root) / f"toolbelt-mutex-{name}.lock"
    if mutex_path in _holds.mutexes:
        raise RuntimeError(f"mutex '{name}' requested while this thread holds it")
    with _flock(mutex_path, operation=fcntl.LOCK_EX, timeout=timeout):
        _holds.mutexes.add(mutex_path)
        try:
            yield
        finally:
            _holds.mutexes.remove(mutex_path)
