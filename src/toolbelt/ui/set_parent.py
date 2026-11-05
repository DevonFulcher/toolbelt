"""Changes a branch's stack parent from the page by running `toolbelt git
set-parent`, exactly as typing it in the branch's worktree would: the command
checks the new parent (it exists, is not the branch itself, forms no cycle),
records it, syncs the stack, pushes, and retargets the branch's open PR. Its
rules are the command's: the page only runs it and reports what it said.

``SetParentTracker`` keeps what the card shows while it runs (the parent being
set) and why it failed, until the next attempt.
"""

import asyncio
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from toolbelt import logged_process
from toolbelt.git.stack.worktree import worktree_paths

#: Longest output kept, the end of it (where an error is).
OUTPUT_LIMIT = 4000
#: Longest reason shown on a card, the end of the command's output.
NOTICE_LIMIT = 600

#: What the command's logger puts before every line.
_LOG_PREFIX = "🔧 "

type BranchKey = tuple[int, str]


@dataclass(frozen=True)
class SetParentOutcome:
    ok: bool
    output: str


def checked_out_worktree(repo_path: Path, branch: str) -> Path | None:
    """The worktree of the repo at ``repo_path`` where ``branch`` is checked out
    (``None``: nowhere), which is where the command has to run."""
    return worktree_paths(root=repo_path).get(branch)


def run_toolbelt_set_parent(worktree: Path, new_parent: str) -> SetParentOutcome:
    """Run `toolbelt git set-parent <new_parent>` in ``worktree``, the checkout
    of the branch whose parent changes."""
    executable = shutil.which("toolbelt") or sys.argv[0]
    try:
        result = logged_process.run(
            [executable, "git", "set-parent", new_parent],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as error:  # noqa: BLE001 - reported to the page
        return SetParentOutcome(ok=False, output=f"{type(error).__name__}: {error}")
    output = (result.stdout + result.stderr).strip()
    return SetParentOutcome(ok=result.returncode == 0, output=output[-OUTPUT_LIMIT:])


def failure_reason(output: str) -> str:
    """What the command said, as the end of it: without its log prefix and
    without the git and gh commands it echoes as it runs them."""
    lines = [line.removeprefix(_LOG_PREFIX) for line in output.splitlines()]
    said = [line for line in lines if not line.startswith(("git ", "gh "))]
    return "\n".join(said).strip()[-NOTICE_LIMIT:]


class SetParentTracker:
    """The parent changes in flight and the reasons the last ones failed.

    ``runner(worktree, new_parent)`` does the work (off the event loop);
    ``changed()`` is called when a change starts and when it ends, so the
    card is told (and, after a success, the stack's new shape is read)."""

    def __init__(
        self,
        *,
        runner: Callable[[Path, str], SetParentOutcome],
        changed: Callable[[], None],
    ) -> None:
        self._runner = runner
        self._changed = changed
        self._setting: dict[BranchKey, str] = {}
        self._notice: dict[BranchKey, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def state(self, repo_id: int, branch: str) -> tuple[str | None, str | None]:
        """``(parent being set, why the last change failed)`` of the branch."""
        key = (repo_id, branch)
        return self._setting.get(key), self._notice.get(key)

    def start(
        self, repo_id: int, branch: str, *, worktree: Path, new_parent: str
    ) -> bool:
        """Starts changing the branch's parent in the background; false when a
        change of it is already running. Needs a running event loop."""
        key = (repo_id, branch)
        if key in self._setting:
            return False
        self._notice.pop(key, None)
        # Marked now, not once the task gets its turn, so nothing else starts it.
        self._setting[key] = new_parent
        task = asyncio.create_task(self._set(key, worktree, new_parent))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._changed()
        return True

    async def _set(self, key: BranchKey, worktree: Path, new_parent: str) -> None:
        try:
            outcome = await asyncio.to_thread(self._runner, worktree, new_parent)
        except Exception as error:  # noqa: BLE001 - reported to the card
            outcome = SetParentOutcome(
                ok=False, output=f"{type(error).__name__}: {error}"
            )
        del self._setting[key]
        if not outcome.ok:
            reason = failure_reason(outcome.output)
            self._notice[key] = reason or "Setting the parent failed."
        self._changed()
