"""Pushing the branches that only this laptop has, before it is wiped.

The export does it so that the new laptop can recreate the worktrees from
origin. Only what is safe and wanted is pushed, and never by force:

* a branch whose PR is merged or closed is stale: its commits are not wanted,
  so it is left alone and reported;
* a branch that already has an upstream gets a plain push (a rejection is
  reported: someone pushed meanwhile);
* a branch with no upstream is pushed only when its PR is open or a draft, so
  the old pre-merge branches of finished work do not turn into remote branches;
* the repository's default branch is pushed only for the dotfiles repos, which
  the new laptop clones before anything else.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from toolbelt import logged_process
from toolbelt.machine_move.in_flight import UnpushedBranch

STALE_PR_STATES = ("merged", "closed")
LIVE_PR_STATES = ("open", "draft")

type Runner = Callable[[Path, list[str]], tuple[int, str]]


@dataclass(frozen=True)
class PushResult:
    repo: str
    branch: str
    #: ``pushed``, ``skipped`` or ``failed``.
    outcome: str
    detail: str

    def __str__(self) -> str:
        return f"{self.repo} [{self.branch}]: {self.outcome}: {self.detail}"


def _git(repo: Path, args: list[str]) -> tuple[int, str]:
    result = logged_process.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _default_branch(repo: Path, git: Runner) -> str | None:
    code, out = git(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    return out.removeprefix("origin/") if code == 0 else None


def push_unpushed(
    branches: list[UnpushedBranch],
    *,
    pr_states: dict[tuple[str, str], str],
    always_push: set[Path],
    git: Runner = _git,
) -> list[PushResult]:
    """Pushes what the rules above allow. ``pr_states`` is the saved PR state
    of each tracked branch, by (repo path, branch); ``always_push`` are the
    repos (the dotfiles ones) whose branches are pushed whatever they are."""
    results: list[PushResult] = []
    for item in branches:
        name = item.repo.name
        state = pr_states.get((str(item.repo), item.branch))
        if item.repo not in always_push:
            if _default_branch(item.repo, git) == item.branch:
                reason = "the default branch is not pushed"
            elif state in STALE_PR_STATES:
                reason = f"its PR is {state}: the commits are stale"
            elif not item.has_upstream and state not in LIVE_PR_STATES:
                reason = "no open PR and no upstream: push it yourself if wanted"
            else:
                reason = None
            if reason is not None:
                results.append(PushResult(name, item.branch, "skipped", reason))
                continue
        args = ["push", "origin", item.branch]
        if not item.has_upstream:
            args = ["push", "-u", "origin", item.branch]
        code, out = git(item.repo, args)
        if code == 0:
            results.append(PushResult(name, item.branch, "pushed", "ok"))
        else:
            last = (out.splitlines() or ["git push failed"])[-1]
            results.append(PushResult(name, item.branch, "failed", last))
    return results
