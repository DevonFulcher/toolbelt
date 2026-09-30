"""Serialize toolbelt's git-mutating commands across processes.

Multiple agents/shells can run toolbelt commands against the same repo at
once — including from different worktrees, since lineage config, branches,
and worktree registration are all shared across a repo's worktrees (they live
in the common ``.git`` dir, not per-worktree). Interleaving two multi-step
commands (``sync``, ``branch-clean``, ``remove``, ...) can leave the stack
lineage inconsistent: a branch reparented by one process while another is
mid-restack onto it, two pushes racing the same branch, and so on.

``repo_lock`` takes an OS-level advisory lock (``flock``) on a file inside
the repo's shared ``.git`` directory, so every worktree of the same repo
contends for the exact same lock, and a second command just waits for the
first to finish rather than interleaving with it. The wait is bounded: a
lock that isn't free within ``timeout`` seconds means whatever holds it is
stuck (or the machine it was on died without a clean shutdown), not just
slow — a timeout can't undo whatever that stuck process left half-done (a
lock has no idea what its holder was doing), but it stops every other
command piling up behind it forever with no signal anything is wrong.

Locking happens only at the CLI command layer (one ``with repo_lock(root):``
per command invocation, wrapping its entire body) — never inside the shared
core functions (``sync_repo``, ``sync_stack``, ``git_branch_clean``,
``compress_branch``, ...), since several of those call each other and
``flock`` is not reentrant: a second lock attempt on the same file from the
same process would just block on itself forever.
"""

import contextlib
import fcntl
import time
from pathlib import Path
from typing import Iterator

import typer

from toolbelt.git.exec import capture
from toolbelt.logger import logger

_LOCK_FILENAME = "toolbelt.lock"
_DEFAULT_TIMEOUT_SECONDS = 300.0
_POLL_INTERVAL_SECONDS = 0.2


@contextlib.contextmanager
def repo_lock(
    root: Path, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Hold an exclusive lock on the repo ``root`` belongs to, for the
    ``with`` block's duration. Blocks (after logging once) if another
    toolbelt command already holds it, anywhere in the repo — up to
    ``timeout`` seconds, after which this exits with an error rather than
    waiting forever.
    """
    git_common_dir = Path(capture(["git", "rev-parse", "--git-common-dir"], cwd=root))
    if not git_common_dir.is_absolute():
        git_common_dir = root / git_common_dir
    lock_path = git_common_dir / _LOCK_FILENAME

    with open(lock_path, "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Waiting for another toolbelt command to finish...")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
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
                    time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
