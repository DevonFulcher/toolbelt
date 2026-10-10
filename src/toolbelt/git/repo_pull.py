"""Background fast-forward pulls of the repos listed under ``sync.repos``.

``$XDG_CONFIG_HOME/toolbelt/config.yaml`` (default
``~/.config/toolbelt/config.yaml``; see ``default_config_path``)::

    sync:
      repos:
        - ~/git/some-repo            # absolute / ``~`` paths used as-is
        - another-repo               # relative: under $GIT_PROJECTS_WORKDIR

``tt git sync`` spawns this module as a detached process
(``python -m toolbelt.git.repo_pull``) so the CLI never waits on the network.
The process writes ``~/.toolbelt/sync-pull-status.json``; the next ``sync``
prints a one-line summary of it once.

A repo is only ever fast-forwarded, and only when that can't touch local
work: clean worktree, default branch checked out, upstream strictly ahead.
Nothing is merged, rebased or stashed.
"""

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from toolbelt import logged_process
from toolbelt.dotfiles.config import config_path

_FALLBACK_DEFAULT_BRANCHES = ("main", "master")


def default_state_dir() -> Path:
    return Path.home() / ".toolbelt"


def default_config_path(env: Mapping[str, str] = os.environ) -> Path:
    """toolbelt's config file (shared with ``tt dotfiles``); see
    ``toolbelt.dotfiles.config.config_path``.

    Config is kept apart from the state dir (``default_state_dir``) because
    only the config is symlinked in from the dotfiles repo.
    """
    return config_path(env, home=Path.home())


def load_sync_repos(config_path: Path, *, workdir: Path | None = None) -> list[Path]:
    """Repos listed under ``sync.repos`` in the YAML file at ``config_path``.

    Relative entries resolve against ``workdir`` (``GIT_PROJECTS_WORKDIR``).
    """
    if not config_path.exists():
        return []
    config = yaml.safe_load(config_path.read_text()) or {}
    entries = (config.get("sync") or {}).get("repos") or []
    repos: list[Path] = []
    for entry in entries:
        path = Path(str(entry)).expanduser()
        if not path.is_absolute():
            if workdir is None:
                raise ValueError(
                    f"sync.repos entry '{entry}' is relative but "
                    "GIT_PROJECTS_WORKDIR is not set"
                )
            path = workdir / path
        repos.append(path)
    return repos


@dataclass
class PullResult:
    repo: str
    outcome: str  # "pulled" | "up-to-date" | "skipped" | "error"
    detail: str = ""


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return logged_process.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _default_branch(path: Path) -> str | None:
    head = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=path)
    if head.returncode == 0:
        return head.stdout.strip().removeprefix("origin/")
    for name in _FALLBACK_DEFAULT_BRANCHES:
        ref = f"refs/heads/{name}"
        if _git(["rev-parse", "--verify", "--quiet", ref], cwd=path).returncode == 0:
            return name
    return None


def pull_repo(path: Path) -> PullResult:
    """Fast-forward ``path``'s default branch if (and only if) that's safe."""
    name = path.name
    if not path.is_dir() or _git(["rev-parse", "--git-dir"], cwd=path).returncode:
        return PullResult(name, "skipped", "not a git repo")
    if _git(["status", "--porcelain"], cwd=path).stdout.strip():
        return PullResult(name, "skipped", "uncommitted changes")
    branch = _git(["symbolic-ref", "--short", "HEAD"], cwd=path).stdout.strip()
    if not branch:
        return PullResult(name, "skipped", "detached HEAD")
    if branch != _default_branch(path):
        return PullResult(name, "skipped", f"on non-default branch '{branch}'")
    if _git(["rev-parse", "--verify", "--quiet", "@{u}"], cwd=path).returncode:
        return PullResult(name, "skipped", f"'{branch}' has no upstream")

    fetch = _git(["fetch", "--quiet"], cwd=path)
    if fetch.returncode != 0:
        return PullResult(name, "error", f"fetch failed: {fetch.stderr.strip()}")
    head = _git(["rev-parse", "HEAD"], cwd=path).stdout.strip()
    upstream = _git(["rev-parse", "@{u}"], cwd=path).stdout.strip()
    if head == upstream:
        return PullResult(name, "up-to-date")
    if _git(["merge-base", "--is-ancestor", "HEAD", "@{u}"], cwd=path).returncode:
        return PullResult(name, "skipped", f"'{branch}' has local commits (diverged)")
    merge = _git(["merge", "--ff-only", "--quiet", "@{u}"], cwd=path)
    if merge.returncode != 0:
        return PullResult(name, "error", merge.stderr.strip())
    return PullResult(name, "pulled")


def run_pulls(repos: list[Path], *, state_dir: Path) -> list[PullResult]:
    """Pull every repo and record the results for the next ``sync`` to report.

    Exits quietly if another run already holds the lock.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "sync-pull.lock", "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        results = [pull_repo(repo) for repo in repos]
        status = {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "reported": False,
            "results": [asdict(r) for r in results],
        }
        (state_dir / "sync-pull-status.json").write_text(json.dumps(status, indent=2))
        return results


def start_background_pull(*, state_dir: Path, config_path: Path) -> bool:
    """Spawn the detached puller if any repos are configured. Never blocks."""
    workdir = os.getenv("GIT_PROJECTS_WORKDIR")
    if not load_sync_repos(config_path, workdir=Path(workdir) if workdir else None):
        return False
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "sync-pull.log", "a") as log:
        logged_process.Popen(
            [sys.executable, "-m", "toolbelt.git.repo_pull"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return True


def take_unreported_summary(*, state_dir: Path) -> str | None:
    """One-line summary of the last background run, once; ``None`` if there's
    nothing new to report."""
    status_path = state_dir / "sync-pull-status.json"
    if not status_path.exists():
        return None
    status = json.loads(status_path.read_text())
    if status["reported"] or not status["results"]:
        return None
    status["reported"] = True
    status_path.write_text(json.dumps(status, indent=2))

    parts: list[str] = []
    for result in status["results"]:
        if result["outcome"] in ("pulled", "up-to-date"):
            parts.append(f"{result['repo']}: {result['outcome']}")
        else:
            parts.append(f"{result['repo']}: {result['outcome']} ({result['detail']})")
    return "Background repo pulls: " + "; ".join(parts)


if __name__ == "__main__":
    _workdir = os.getenv("GIT_PROJECTS_WORKDIR")
    _state_dir = default_state_dir()
    run_pulls(
        load_sync_repos(
            default_config_path(),
            workdir=Path(_workdir) if _workdir else None,
        ),
        state_dir=_state_dir,
    )
