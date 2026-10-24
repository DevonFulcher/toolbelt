"""A task's personal review: the step between "validated" and "PR".

A validate run (``autonomous_agents``, trigger ``validate``) reviews a task's
branch against its parent before any PR exists. When one ends without a
pending question the task is *validated* and its next action is **Personal
review** (``next_action.PERSONAL_REVIEW``): you read the diff in the Review
view and either approve it or request changes with a note. Approving pushes the
branch and opens the PR for review; requesting changes sends the task back to
"Open PR" until the next validate run ends. A verdict is for the head commit it
was given at: an approval of an older commit does not open a PR.

Everything the next action reads is in tables (``agent_runs``,
``review_verdicts``), so the card and the aggregate view replay it the same
way: ``validation_spans`` is that replay.
"""

import asyncio
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path

from toolbelt import logged_process
from toolbelt.autonomous_agents.limits import VALIDATE
from toolbelt.git.stack.store import AgentRun, ReviewVerdict, StackStore
from toolbelt.logger import logger

APPROVED = "approved"
CHANGES_REQUESTED = "changes_requested"

type BranchKey = tuple[int, str]


def validation_spans(
    runs: Sequence[AgentRun], verdicts: Sequence[ReviewVerdict]
) -> list[tuple[datetime, datetime | None]]:
    """When a task counted as validated: from the end of a validate run that
    succeeded until changes were requested (``None``: still). A run that ends
    while the task is validated changes nothing."""
    moments: list[tuple[datetime, bool]] = [
        (run.ended_at, True)
        for run in runs
        if run.trigger == VALIDATE and run.state == "succeeded" and run.ended_at
    ]
    moments += [(v.at, False) for v in verdicts if v.verdict == CHANGES_REQUESTED]
    spans: list[tuple[datetime, datetime | None]] = []
    start: datetime | None = None
    for at, validated in sorted(moments, key=lambda moment: moment[0]):
        if validated and start is None:
            start = at
        elif not validated and start is not None:
            spans.append((start, at))
            start = None
    if start is not None:
        spans.append((start, None))
    return spans


def validated_tasks(store: StackStore) -> set[str]:
    """The ids of the tasks that are validated now."""
    runs: dict[str, list[AgentRun]] = {}
    for run in store.agent_runs():
        if run.trigger == VALIDATE:
            runs.setdefault(run.task_id, []).append(run)
    verdicts: dict[str, list[ReviewVerdict]] = {}
    for verdict in store.review_verdicts():
        verdicts.setdefault(verdict.task_id, []).append(verdict)
    return {
        task_id
        for task_id, task_runs in runs.items()
        if (spans := validation_spans(task_runs, verdicts.get(task_id, [])))
        and spans[-1][1] is None
    }


def current_verdict(
    verdicts: Sequence[ReviewVerdict], head_sha: str
) -> ReviewVerdict | None:
    """The latest verdict that still stands: an approval only for ``head_sha``;
    a request for changes stands until a validate run ends."""
    if not verdicts:
        return None
    latest = verdicts[-1]
    if latest.verdict == APPROVED and latest.head_sha != head_sha:
        return None
    return latest


class ReviewRefused(Exception):
    """The verdict cannot be given (shown to the user as is)."""


type OpenPr = Callable[[Path, str, str], str]


def open_pr(
    repo: Path,
    branch: str,
    base: str,
    run_process: Callable[..., subprocess.CompletedProcess[str]] = logged_process.run,
) -> str:
    """Pushes ``branch`` to origin (never forced) and opens its PR for review
    (not a draft) against ``base``, filled in from its commits; returns the
    PR's URL. ``run_process`` starts ``git`` and ``gh``."""
    pushed = run_process(
        ["git", "push", "-u", "origin", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if pushed.returncode:
        raise ReviewRefused(f"Pushing {branch} failed: {pushed.stderr.strip()}")
    created = run_process(
        ["gh", "pr", "create", "--base", base, "--head", branch, "--fill"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if created.returncode:
        raise ReviewRefused(f"Opening the PR failed: {created.stderr.strip()}")
    return str(created.stdout).strip().splitlines()[-1]


class PersonalReview:
    """Records the verdicts and, on an approval, opens the PR in the
    background (git and GitHub take seconds: see ``docs/async-ui.md``)."""

    def __init__(
        self,
        *,
        open_store: Callable[[], StackStore],
        repo_path: Callable[[int], Path | None],
        head_sha: Callable[[Path, str, str], str],
        base_of: Callable[[int, str], str | None],
        open_pr: OpenPr,
        opened: Callable[[int, str, bool], Awaitable[None]],
        wall_clock: Callable[[], datetime],
        changed: Callable[[BranchKey], None],
    ) -> None:
        """``head_sha(repo, base, branch)`` is the commit the Review view shows;
        ``opened(repo_id, branch, post_in_slack)`` runs once the PR exists (it
        records it and posts in Slack when asked); ``changed`` is told when a
        branch's personal review changed, for the page."""
        self._open_store = open_store
        self._repo_path = repo_path
        self._head_sha = head_sha
        self._base_of = base_of
        self._open_pr = open_pr
        self._opened = opened
        self._wall_clock = wall_clock
        self._changed = changed
        #: The branches whose PR is being opened, and why one failed.
        self.opening: set[BranchKey] = set()
        self.notices: dict[BranchKey, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def head(self, repo_id: int, branch: str) -> str:
        repo = self._repo_path(repo_id)
        base = self._base_of(repo_id, branch)
        if repo is None or base is None:
            raise ReviewRefused("This branch is not tracked.")
        return self._head_sha(repo, base, branch)

    def _record(self, key: BranchKey, head_sha: str, verdict: str, note: str) -> str:
        store = self._open_store()
        try:
            task = next(
                (t for t in store.tracked_branch_facts() if (t.repo_id, t.name) == key),
                None,
            )
            if task is None:
                raise ReviewRefused("This branch is not tracked.")
            if task.pr is not None:
                raise ReviewRefused("This task already has a PR.")
            if task.task_id not in validated_tasks(store):
                raise ReviewRefused("This task is not waiting for a personal review.")
            store.add_review_verdict(
                task_id=task.task_id,
                repo_id=key[0],
                branch=key[1],
                head_sha=head_sha,
                verdict=verdict,
                note=note,
                at=self._wall_clock(),
            )
            return task.task_id
        finally:
            store.close()

    def request_changes(self, key: BranchKey, head_sha: str, note: str) -> None:
        self._record(key, head_sha, CHANGES_REQUESTED, note)
        self.notices.pop(key, None)
        self._changed(key)

    def check(self, key: BranchKey, head_sha: str) -> None:
        """Raises when ``head_sha`` is not the branch's head now: the page showed
        an older commit. Runs git, so not on the event loop."""
        if self.head(*key) != head_sha:
            raise ReviewRefused(
                "The branch has a new commit since this page loaded: reload it."
            )

    def approve(
        self, key: BranchKey, head_sha: str, note: str, post_in_slack: bool
    ) -> None:
        """Records the approval and starts opening the PR; returns at once."""
        if key in self.opening:
            raise ReviewRefused("The PR is already being opened.")
        self._record(key, head_sha, APPROVED, note)
        self.opening.add(key)
        self.notices.pop(key, None)
        self._changed(key)
        task = asyncio.create_task(self._open(key, post_in_slack))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _open(self, key: BranchKey, post_in_slack: bool) -> None:
        repo_id, branch = key
        try:
            repo = self._repo_path(repo_id)
            base = self._base_of(repo_id, branch)
            if repo is None or base is None:
                raise ReviewRefused("This branch is not tracked.")
            await asyncio.to_thread(self._open_pr, repo, branch, base)
            await self._opened(repo_id, branch, post_in_slack)
        except Exception as error:  # noqa: BLE001 - shown on the task's card
            logger.exception("opening the PR of %s failed", branch)
            self.notices[key] = str(error)
        finally:
            self.opening.discard(key)
            self._changed(key)

    async def idle(self) -> None:
        """Returns when no PR is being opened."""
        while self._tasks:
            await asyncio.gather(*self._tasks)
