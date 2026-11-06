"""Removes a branch from the page by running `toolbelt git remove`, exactly as
typing it in the repo's main checkout would: the command removes the branch's
worktree, force-deletes the branch, reparents its children onto its parent,
closes its open PR (with the reason as a comment) and drops it from the stack.
Its rules are the command's (a worktree with uncommitted files is refused
unless forced): the page only runs it and reports what it said.

``RemoveTracker`` keeps what the card shows while it runs and why it failed,
until the next attempt.
"""

import asyncio
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from toolbelt import logged_process
from toolbelt.ui.set_parent import OUTPUT_LIMIT, failure_reason

type BranchKey = tuple[int, str]


@dataclass(frozen=True)
class RemoveRequest:
    """What to run: the repo's main checkout, the branch, and the command's
    ``--force`` and ``--reason``."""

    repo_path: Path
    branch: str
    force: bool
    reason: str | None


@dataclass(frozen=True)
class RemoveOutcome:
    ok: bool
    output: str


def run_toolbelt_remove(request: RemoveRequest) -> RemoveOutcome:
    """Run `toolbelt git remove <branch>` in the repo's main checkout: not in
    the branch's own worktree, which the command deletes."""
    executable = shutil.which("toolbelt") or sys.argv[0]
    command = [executable, "git", "remove", request.branch]
    if request.force:
        command.append("--force")
    if request.reason:
        command += ["--reason", request.reason]
    try:
        result = logged_process.run(
            command,
            cwd=request.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as error:  # noqa: BLE001 - reported to the page
        return RemoveOutcome(ok=False, output=f"{type(error).__name__}: {error}")
    output = (result.stdout + result.stderr).strip()
    return RemoveOutcome(ok=result.returncode == 0, output=output[-OUTPUT_LIMIT:])


class RemoveTracker:
    """The removals in flight and the reasons the last ones failed.

    ``runner(request)`` does the work (off the event loop); ``changed()`` is
    called when a removal starts and when it ends, so the card is told (and,
    after a success, the stack without the branch is read)."""

    def __init__(
        self,
        *,
        runner: Callable[[RemoveRequest], RemoveOutcome],
        changed: Callable[[], None],
    ) -> None:
        self._runner = runner
        self._changed = changed
        self._removing: set[BranchKey] = set()
        self._notice: dict[BranchKey, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def state(self, repo_id: int, branch: str) -> tuple[bool, str | None]:
        """``(being removed, why the last removal failed)`` of the branch."""
        key = (repo_id, branch)
        return key in self._removing, self._notice.get(key)

    def start(self, repo_id: int, request: RemoveRequest) -> bool:
        """Starts removing the branch in the background; false when its removal
        is already running. Needs a running event loop."""
        key = (repo_id, request.branch)
        if key in self._removing:
            return False
        self._notice.pop(key, None)
        # Marked now, not once the task gets its turn, so nothing else starts it.
        self._removing.add(key)
        task = asyncio.create_task(self._remove(key, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._changed()
        return True

    async def _remove(self, key: BranchKey, request: RemoveRequest) -> None:
        try:
            outcome = await asyncio.to_thread(self._runner, request)
        except Exception as error:  # noqa: BLE001 - reported to the card
            outcome = RemoveOutcome(ok=False, output=f"{type(error).__name__}: {error}")
        self._removing.discard(key)
        if not outcome.ok:
            reason = failure_reason(outcome.output)
            self._notice[key] = reason or "Removing the branch failed."
        self._changed()
