"""A task's personal review: the step between "validated" and "PR".

A validate run (``autonomous_agents``, trigger ``validate``) reviews a task's
branch against its parent before any PR exists. When one succeeds without a
pending question the task is *validated* and its next action is **Personal
review** (``next_action.PERSONAL_REVIEW``): you read each changed file in the
Review view and approve it, or request changes to it with a note. When every
file of the branch's diff is approved the branch is pushed and its PR opened.

A decision is for one file and the hash of that file's own diff against the
parent (``revisions.RevisionPair.file_diffs``), not for a commit. A new commit
elsewhere, or a rebase onto a moved parent that leaves the file's diff as it
was, keeps the decision; any change to the file's diff drops it back to
unreviewed. A file that leaves the diff drops out, a file that enters it starts
unreviewed, and the task is approved only when every file of its current diff
is (and the diff is not empty).

Everything the next action reads is in tables (``agent_runs``,
``file_approvals``), so the card and the aggregate view replay it the same way.
"""

import asyncio
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from toolbelt import logged_process
from toolbelt.autonomous_agents.limits import VALIDATE
from toolbelt.diff_review.revisions import FileDiff, RevisionError
from toolbelt.git.stack.store import AgentRun, FileApproval, StackStore
from toolbelt.logger import logger

APPROVED = "approved"
CHANGES_REQUESTED = "changes_requested"
UNREVIEWED = "unreviewed"

type BranchKey = tuple[int, str]


def validated_since(runs: Sequence[AgentRun]) -> datetime | None:
    """When the task became validated: the end of its first validate run that
    succeeded (``None``: not yet). It stays validated until its PR exists."""
    ended = [
        run.ended_at
        for run in runs
        if run.trigger == VALIDATE and run.state == "succeeded" and run.ended_at
    ]
    return min(ended) if ended else None


def validated_tasks(store: StackStore) -> set[str]:
    """The ids of the tasks that are validated."""
    return {
        run.task_id
        for run in store.agent_runs()
        if run.trigger == VALIDATE and run.state == "succeeded"
    }


@dataclass(frozen=True)
class FileReview:
    """A file of the branch's current diff and where its review stands."""

    diff: FileDiff
    state: str
    note: str


def file_reviews(
    diffs: Sequence[FileDiff], approvals: Sequence[FileApproval]
) -> list[FileReview]:
    """Each file of ``diffs`` with the latest decision made for that path and
    that diff hash (``approvals`` oldest first); a decision for another diff of
    the file, or for a file no longer in the diff, does not count."""
    latest = {(a.path, a.diff_hash): a for a in approvals}
    return [
        FileReview(diff, found.state, found.note)
        if (found := latest.get((diff.path, diff.diff_hash)))
        else FileReview(diff, UNREVIEWED, "")
        for diff in diffs
    ]


def fully_approved(reviews: Sequence[FileReview]) -> bool:
    return bool(reviews) and all(review.state == APPROVED for review in reviews)


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
    """Records the decisions and, once every file is approved, opens the PR in
    the background (git and GitHub take seconds: see ``docs/async-ui.md``)."""

    def __init__(
        self,
        *,
        open_store: Callable[[], StackStore],
        repo_path: Callable[[int], Path | None],
        file_diffs: Callable[[Path, str, str], list[FileDiff]],
        base_of: Callable[[int, str], str | None],
        open_pr: OpenPr,
        opened: Callable[[int, str, bool], Awaitable[None]],
        wall_clock: Callable[[], datetime],
        changed: Callable[[BranchKey], None],
    ) -> None:
        """``file_diffs(repo, base, branch)`` is the files of the branch's diff
        with their hashes; ``opened(repo_id, branch, post_in_slack)`` runs once
        the PR exists (it looks it up and posts in Slack when asked);
        ``changed`` is told when a branch's personal review changed, for the
        page."""
        self._open_store = open_store
        self._repo_path = repo_path
        self._file_diffs = file_diffs
        self._base_of = base_of
        self._open_pr = open_pr
        self._opened = opened
        self._wall_clock = wall_clock
        self._changed = changed
        #: The branches whose PR is being opened, and why one failed.
        self.opening: set[BranchKey] = set()
        self.notices: dict[BranchKey, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def diffs(self, key: BranchKey) -> list[FileDiff]:
        """The files of the branch's diff against its parent now. Runs git, so
        not on the event loop."""
        repo = self._repo_path(key[0])
        base = self._base_of(*key)
        if repo is None or base is None:
            raise ReviewRefused("This branch is not tracked.")
        try:
            return self._file_diffs(repo, base, key[1])
        except RevisionError as error:
            raise ReviewRefused(str(error)) from error

    def reviews(self, key: BranchKey) -> list[FileReview]:
        diffs = self.diffs(key)
        store = self._open_store()
        try:
            task = self._task(store, key)
            return file_reviews(diffs, store.file_approvals(task_id=task))
        finally:
            store.close()

    def _task(self, store: StackStore, key: BranchKey) -> str:
        """The task awaiting its review on ``key``'s branch."""
        found = next(
            (t for t in store.tracked_branch_facts() if (t.repo_id, t.name) == key),
            None,
        )
        if found is None:
            raise ReviewRefused("This branch is not tracked.")
        if found.pr is not None:
            raise ReviewRefused("This task already has a PR.")
        if found.task_id not in validated_tasks(store):
            raise ReviewRefused("This task is not waiting for a personal review.")
        return found.task_id

    def _record(
        self, key: BranchKey, decisions: list[tuple[str, str, str, str]]
    ) -> bool:
        """Writes ``(path, diff_hash, state, note)`` decisions against the
        branch's current diff, refusing any that is not for it; returns whether
        every file is approved after them."""
        store = self._open_store()
        try:
            self._task(store, key)
        finally:
            store.close()
        diffs = {d.path: d for d in self.diffs(key)}
        for path, diff_hash, _, _ in decisions:
            if path not in diffs:
                raise ReviewRefused(f"{path} is not in this branch's diff.")
            if diffs[path].diff_hash != diff_hash:
                raise ReviewRefused(
                    f"{path} changed since this page loaded: reload it."
                )
        store = self._open_store()
        try:
            task = self._task(store, key)
            for path, diff_hash, state, note in decisions:
                store.add_file_approval(
                    task_id=task,
                    repo_id=key[0],
                    branch=key[1],
                    path=path,
                    diff_hash=diff_hash,
                    state=state,
                    note=note,
                    at=self._wall_clock(),
                )
            return fully_approved(
                file_reviews(list(diffs.values()), store.file_approvals(task_id=task))
            )
        finally:
            store.close()

    def decide(
        self,
        key: BranchKey,
        path: str,
        diff_hash: str,
        state: str,
        note: str,
    ) -> bool:
        """Approves or requests changes to one file's diff as the page showed
        it; returns whether every file is approved now (call ``finish`` then).
        Runs git, so not on the event loop."""
        if key in self.opening:
            raise ReviewRefused("The PR is already being opened.")
        return self._record(key, [(path, diff_hash, state, note)])

    def approve_all(
        self,
        key: BranchKey,
        shown: dict[str, str],
        note: str,
    ) -> bool:
        """Approves every file of the diff as the page showed it (``shown``:
        path to hash); refused when the diff is not that one. Returns whether
        every file is approved now (always, when it succeeds)."""
        if key in self.opening:
            raise ReviewRefused("The PR is already being opened.")
        current = {d.path: d.diff_hash for d in self.diffs(key)}
        if current != shown:
            raise ReviewRefused("The diff changed since this page loaded: reload it.")
        store = self._open_store()
        try:
            task = self._task(store, key)
            done = {
                r.diff.path
                for r in file_reviews(
                    self.diffs(key), store.file_approvals(task_id=task)
                )
                if r.state == APPROVED
            }
        finally:
            store.close()
        return self._record(
            key,
            [
                (path, h, APPROVED, note)
                for path, h in current.items()
                if path not in done
            ],
        )

    def finish(self, key: BranchKey, complete: bool, post_in_slack: bool) -> None:
        """Tells the page the decision was recorded and, when every file is
        approved, starts opening the PR in the background. On the event loop."""
        self.notices.pop(key, None)
        if complete:
            self.opening.add(key)
        self._changed(key)
        if complete:
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
