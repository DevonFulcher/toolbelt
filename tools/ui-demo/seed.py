"""Seed a throwaway toolbelt home with the demo repos and stacks.

    uv run python tools/ui-demo/seed.py DEST [--deep N]

Creates DEST/repos/{demo-app,demo-search} (empty git repos with an origin
URL, so the aggregate view can match their PRs), DEST/home (a TOOLBELT_HOME
with stacks.db) and DEST/config (an empty XDG_CONFIG_HOME), then registers the
stacks the fake `gh` (tools/ui-demo/gh) has pull requests for. `--deep N` adds
an N-branch linear chain under demo-search/search-ranking to exercise the
depth cap and focus-root navigation.

It also writes the aggregate view's history (see history.py):
DEST/history.json, which the fake `gh` serves to the aggregate search, and the
archived task facts of cleaned-up branches in stacks.db (`removed_branches`).

Run it with TOOLBELT_HOME pointing at DEST/home (run.sh does this).
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from history import RECORDED_WEEKS, history, node_id  # noqa: E402
from toolbelt import logged_process  # noqa: E402
from toolbelt.git.stack import lineage  # noqa: E402
from toolbelt.git.stack.store import PrCandidate, PrLink  # noqa: E402

# Tracked demo branches whose PR the CLI has already recorded (the others have
# a PR but no recorded link yet, as a branch that was never synced since).
LINKED_PRS = {"devon/auth-models": 101, "devon/auth-api": 102, "devon/cache-layer": 88}

SESSION = "claude://claude.ai/epitaxy/local_28e13856-24b4-4207-b16c-7235010e72{n:02d}"

# repo -> [(branch, parent, task, session number or None, task started N days ago)]
STACKS: dict[str, list[tuple[str, str, str | None, int | None, float]]] = {
    "demo-app": [
        ("devon/auth-models", "main", "PLAT-412", 1, 9),
        ("devon/auth-api", "devon/auth-models", "PLAT-412", 2, 6.5),
        ("devon/auth-ui", "devon/auth-api", "PLAT-415", 3, 4),
        ("devon/auth-docs", "devon/auth-api", None, None, 3),
        ("devon/cache-layer", "main", "PLAT-377", 4, 12),
        ("devon/cache-metrics", "devon/cache-layer", "PLAT-377", 5, 3),
    ],
    "demo-search": [
        ("devon/search-index", "main", "SRCH-9", 6, 3),
        ("devon/search-ranking", "devon/search-index", None, None, 2),
    ],
}


def _git(repo: Path, *args: str) -> None:
    logged_process.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def write_history(dest: Path, db: sqlite3.Connection, now: datetime) -> None:
    """The aggregate view's history: DEST/history.json for the fake `gh`, and
    the archived task facts in the stack DB (see history.py)."""
    prs, tasks = history(now, random.Random(20261011))
    (dest / "history.json").write_text(json.dumps(prs, indent=1))
    for task in tasks:
        row = db.execute(
            "SELECT id FROM repos WHERE name = ?", (task["repo"],)
        ).fetchone()
        db.execute(
            "INSERT INTO removed_branches "
            "(task_id, pr_number, pr_node_id, repo_id, name, started_at, "
            "removed_at, landed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                task["pr"]["number"] if task["pr"] else None,
                task["pr"]["node_id"] if task["pr"] else None,
                row[0],
                task["branch"],
                task["started"].isoformat(),
                task["removed"].isoformat(),
                int(task["landed"]),
            ),
        )
    # Removals before this were never archived, so their tasks have no start.
    since = (now - timedelta(weeks=RECORDED_WEEKS)).isoformat()
    db.execute("UPDATE archive_info SET since = ?", (since,))


def seed(dest: Path, deep: int) -> None:
    repos = dest / "repos"
    for name in STACKS:
        repo = repos / name
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q", "-b", "main")
        # So the aggregate view can match a repo's PRs to its branches.
        _git(repo, "remote", "add", "origin", f"https://github.com/devon/{name}.git")
        _git(
            repo,
            "-c",
            "user.name=Demo",
            "-c",
            "user.email=demo@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        )
    (dest / "config").mkdir(exist_ok=True)

    started: dict[tuple[str, str], float] = {}
    for name, branches in STACKS.items():
        for branch, parent, task, session, days in branches:
            lineage.set_parent(
                branch,
                parent,
                root=repos / name,
                task=task,
                agent_link=SESSION.format(n=session) if session else None,
            )
            started[(name, branch)] = days
    for name, branches in STACKS.items():
        for branch, *_ in branches:
            if branch in LINKED_PRS:
                number = LINKED_PRS[branch]
                lineage.link_pr(
                    branch,
                    [
                        PrCandidate(
                            PrLink(number, node_id(number)),
                            datetime.now(timezone.utc) - timedelta(days=30),
                        )
                    ],
                    root=repos / name,
                )
    parent = "devon/search-ranking"
    for i in range(1, deep + 1):
        branch = f"devon/deep-{i:02d}"
        lineage.set_parent(branch, parent, root=repos / "demo-search")
        parent = branch

    # `started_at` is stamped "now" on insert; backdate the demo branches.
    db = sqlite3.connect(Path(os.environ["TOOLBELT_HOME"]) / "stacks.db")
    now = datetime.now(timezone.utc)
    for (_, branch), days in started.items():
        when = (now - timedelta(days=days)).isoformat()
        db.execute("UPDATE branches SET started_at = ? WHERE name = ?", (when, branch))
    write_history(dest, db, now)
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--deep", type=int, default=0, metavar="N")
    args = parser.parse_args()
    seed(args.dest.resolve(), args.deep)


if __name__ == "__main__":
    main()
