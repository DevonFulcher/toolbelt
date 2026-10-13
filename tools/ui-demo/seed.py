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

It writes task claims (what a Claude Code session claims; docs/task-claims.md)
into DEST/home/claims: a working holder on a branch with a failing CI, an idle
one, a blocked one, an expired one and a revoked one. The live holders have
an 8-hour ttl so they outlast a look at the demo (a real writer's is 3 min).

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


def write_claims(home: Path, now: datetime) -> None:
    """Claim files in the contract's shape (docs/task-claims.md)."""

    def z(time: datetime) -> str:
        return time.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    claims = home / "claims"
    (claims / "revoked").mkdir(parents=True, exist_ok=True)

    def holder(
        name: str,
        state: str,
        refs: list[str],
        *,
        updated: datetime,
        ttl: int,
        touched: datetime | None = None,
        session: int | None = None,
    ) -> None:
        touched = touched or updated
        document = {
            "version": 1,
            "holder": name,
            "kind": "agent",
            "session_id": name.split(".")[0],
            "agent_id": name.split(".")[1] if "." in name else None,
            "link": SESSION.format(n=session) if session else None,
            "state": state,
            "started_at": z(updated - timedelta(minutes=20)),
            "updated_at": z(updated),
            "ttl_seconds": ttl,
            "claims": [
                {"kind": "ref", "ref": ref, "touched_at": z(touched)} for ref in refs
            ],
        }
        (claims / f"{name}.json").write_text(json.dumps(document, indent=2))

    day = 8 * 3600
    # Working on a branch whose CI is failing: reads "With an agent", and
    # would be "Fix / re-run CI".
    holder(
        "sess-working", "working", ["devon/auth-docs"], updated=now, ttl=day, session=11
    )
    # Waiting for you / on a prompt: the status is unchanged, a chip is added.
    holder("sess-idle", "idle", ["devon/cache-layer"], updated=now, ttl=day, session=12)
    holder(
        "sess-blocked", "blocked", ["devon/auth-ui"], updated=now, ttl=day, session=13
    )
    # Expired: ignored (and deleted once past the grace).
    holder(
        "sess-expired",
        "working",
        ["devon/search-index"],
        updated=now - timedelta(hours=2),
        ttl=180,
        session=14,
    )
    # Alive, but its claim was released with `tt task release`.
    holder(
        "sess-revoked",
        "working",
        ["devon/cache-metrics"],
        updated=now,
        ttl=day,
        touched=now - timedelta(minutes=5),
        session=15,
    )
    (claims / "revoked" / "sess-revoked.json").write_text(
        json.dumps(
            {
                "version": 1,
                "holder": "sess-revoked",
                "revoked": [
                    {
                        "kind": "ref",
                        "ref": "devon/cache-metrics",
                        "at": z(now - timedelta(minutes=1)),
                        "by": "tt task release",
                    }
                ],
            },
            indent=2,
        )
    )


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
    write_claims(Path(os.environ["TOOLBELT_HOME"]), now)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--deep", type=int, default=0, metavar="N")
    args = parser.parse_args()
    seed(args.dest.resolve(), args.deep)


if __name__ == "__main__":
    main()
