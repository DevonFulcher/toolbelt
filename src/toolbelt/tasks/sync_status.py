"""Keeps a task's status in step with its branch: moved when work starts
(``toolbelt git append``) and when it lands (``toolbelt git merge``).

Best-effort by design: the git command has done its job whether or not the
tracker could be updated, so a failure is a warning, never an error.
"""

from toolbelt.logger import logger
from toolbelt.tasks.tracker import Stage, TaskTracker


def sync_task_status(
    tracker: TaskTracker | None, task: str | None, stage: Stage
) -> None:
    """Moves ``task`` to its tracker's status for ``stage``, and says what
    happened. Does nothing without a task or a tracker."""
    if tracker is None or task is None:
        return
    result = tracker.move(task, stage)
    if result.outcome == "failed":
        logger.warning(f"Task status not updated: {result.message}")
    else:
        logger.info(f"Task status: {result.message}")
