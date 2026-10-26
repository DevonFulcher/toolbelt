"""Work that lives only on the old laptop: git does not carry it.

Uncommitted changes, commits no remote has, and stashes are found per repo
(every worktree of it) and listed by the export as a checklist: push or commit
them before the old laptop is wiped.
"""

from dataclasses import dataclass
from pathlib import Path

from toolbelt import logged_process
from toolbelt.machine_move.archive_layout import WorktreeRecord


@dataclass(frozen=True)
class InFlightItem:
    repo: str
    where: str
    branch: str | None
    problem: str
    #: Nothing to do about it before the move (stashes, which are not carried).
    informational: bool = False

    def __str__(self) -> str:
        branch = f" [{self.branch}]" if self.branch else ""
        return f"{self.where}{branch}: {self.problem}"


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None
    is_main: bool


def _git(repo: Path, *args: str) -> tuple[int, str]:
    result = logged_process.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return result.returncode, result.stdout


def worktrees(repo: Path) -> list[Worktree]:
    """Every worktree of ``repo``'s repository, the main checkout first."""
    code, out = _git(repo, "worktree", "list", "--porcelain")
    if code != 0:
        return []
    found: list[Worktree] = []
    for block in out.strip().split("\n\n"):
        path: Path | None = None
        branch: str | None = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree "))
            elif line.startswith("branch refs/heads/"):
                branch = line.removeprefix("branch refs/heads/")
        if path is not None and path.exists():
            found.append(Worktree(path, branch, is_main=not found))
    return found


def _unpushed(repo: Path, branch: str) -> int:
    code, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"{branch}@{{u}}")
    if code == 0:
        _, out = _git(repo, "rev-list", "--count", f"{branch}@{{u}}..{branch}")
    else:
        # No upstream: whatever no remote-tracking branch has.
        _, out = _git(repo, "rev-list", "--count", branch, "--not", "--remotes")
    return int(out.strip() or 0)


def find_in_flight(
    repos: list[Path], *, tracked: dict[Path, list[str]]
) -> tuple[list[InFlightItem], list[WorktreeRecord]]:
    """The in-flight items of ``repos`` and the worktrees to recreate.

    ``tracked`` lists, per repo, the branches toolbelt tracks: their unpushed
    commits count even when no worktree has them checked out.
    """
    items: list[InFlightItem] = []
    records: list[WorktreeRecord] = []
    trees_by_main: dict[Path, list[Worktree]] = {}
    tracked_by_main: dict[Path, list[str]] = {}
    for repo in repos:
        trees = worktrees(repo) if repo.exists() else []
        if trees:
            trees_by_main.setdefault(trees[0].path, trees)
            tracked_by_main.setdefault(trees[0].path, []).extend(tracked.get(repo, []))
    for main, trees in trees_by_main.items():
        checked_out = {tree.branch for tree in trees if tree.branch}
        for tree in trees:
            _, status = _git(tree.path, "status", "--porcelain")
            changed = len(status.splitlines())
            if changed:
                items.append(
                    InFlightItem(
                        main.name,
                        str(tree.path),
                        tree.branch,
                        f"{changed} file(s) with uncommitted changes",
                    )
                )
            if tree.branch and not tree.is_main:
                records.append(WorktreeRecord(str(main), str(tree.path), tree.branch))
        branches = [*sorted(checked_out), *tracked_by_main[main]]
        for branch in dict.fromkeys(branches):
            ahead = _unpushed(main, branch)
            if ahead:
                items.append(
                    InFlightItem(
                        main.name, str(main), branch, f"{ahead} commit(s) not pushed"
                    )
                )
        _, stashes = _git(main, "stash", "list")
        count = len(stashes.splitlines())
        if count:
            items.append(
                InFlightItem(
                    main.name,
                    str(main),
                    None,
                    f"{count} stash(es), not carried",
                    informational=True,
                )
            )
    return items, records


@dataclass(frozen=True)
class UnpushedBranch:
    repo: Path
    branch: str
    #: Its upstream branch, when it has one.
    has_upstream: bool


def unpushed_branches(
    repos: list[Path], *, tracked: dict[Path, list[str]]
) -> list[UnpushedBranch]:
    """The branches (checked out in a worktree, or tracked by toolbelt) with
    commits no remote has, once per repository."""
    found: list[UnpushedBranch] = []
    seen: set[Path] = set()
    for repo in repos:
        trees = worktrees(repo) if repo.exists() else []
        if not trees or trees[0].path in seen:
            continue
        main = trees[0].path
        seen.add(main)
        branches = [t.branch for t in trees if t.branch] + tracked.get(repo, [])
        for branch in dict.fromkeys(branches):
            if _unpushed(main, branch):
                code, _ = _git(
                    main, "rev-parse", "--verify", "--quiet", f"{branch}@{{u}}"
                )
                found.append(UnpushedBranch(main, branch, has_upstream=code == 0))
    return found
