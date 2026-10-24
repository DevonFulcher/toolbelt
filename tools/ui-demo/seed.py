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

It records what `tt ui` would have seen of the demo branches, one row set per
lane of the card timelines (docs/timeline-lanes.md): conflict, CI and review
thread changes, agent runs, claim spans, Slack posts, a queued merge,
decisions and a task tracker move (`write_lanes`).

It writes the agent activity the plugin would have recorded: a few weeks of
turns as chunk files in DEST/home/activity (`write_activity`), which `tt ui`
reads when it starts.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from history import RECORDED_WEEKS, history, node_id

from toolbelt import logged_process
from toolbelt.decisions.model import Decision
from toolbelt.finish_estimate.model import MODEL_VERSION
from toolbelt.git.stack import lineage
from toolbelt.git.stack.store import (
    MergeQueueRow,
    PrCandidate,
    PrLink,
    SlackPostRow,
    StackStore,
    TokenCounts,
    default_db_path,
)

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
        ("devon/auth-sso", "main", "PLAT-420", None, 2),
        ("devon/auth-audit", "main", "PLAT-421", None, 2),
        ("devon/auth-lockout", "main", "PLAT-422", None, 1),
    ],
    "demo-search": [
        ("devon/search-index", "main", "SRCH-9", 6, 3),
        ("devon/search-ranking", "devon/search-index", None, None, 2),
        ("devon/search-cache", "main", "SRCH-12", None, 1),
    ],
}

# Branches with no PR that have real commits, so the Review view has a diff and
# an approval can push them: (repo, branch, parent, file, text).
LOCAL_BRANCHES = [
    (
        "demo-app",
        "devon/auth-sso",
        "main",
        "sso.py",
        "def sign_in(provider, token):\n    return provider.verify(token)\n",
    ),
    (
        "demo-app",
        "devon/auth-audit",
        "main",
        "audit.py",
        "AUDITED = ['login', 'logout']\n",
    ),
    (
        "demo-app",
        "devon/auth-lockout",
        "main",
        "lockout.py",
        "def locked(attempts):\n    return attempts >= 5\n",
    ),
]


def _git(repo: Path, *args: str) -> None:
    logged_process.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def write_branches(dest: Path) -> None:
    """The commits of ``LOCAL_BRANCHES``, and a bare repo as demo-search's push
    URL: approving a branch pushes it (never to GitHub) and the fake `gh`
    opens its PR."""
    repos = dest / "repos"
    bare = dest / "origin" / "demo-app.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(repos, "init", "-q", "--bare", str(bare))
    _git(repos / "demo-app", "config", "remote.origin.pushurl", str(bare))
    for name, branch, parent, filename, text in LOCAL_BRANCHES:
        repo = repos / name
        _git(repo, "switch", "-q", "-c", branch, parent)
        (repo / filename).write_text(text)
        _git(repo, "add", filename)
        _git(
            repo,
            "-c",
            "user.name=Demo",
            "-c",
            "user.email=demo@example.com",
            "commit",
            "-q",
            "-m",
            f"Add {filename}",
        )
        _git(repo, "switch", "-q", "main")


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


def write_estimates(db: sqlite3.Connection, now: datetime) -> None:
    """Earlier nightly backtests, so the aggregate view's "Estimate accuracy"
    has a history to draw. They are made up (the demo has no earlier nights);
    the stage history and the backtest of the demo's own data are computed when
    `tt ui` starts, and the newest backtest is that one."""
    stages = {
        "wait_review": (60, 0.78, 5.1 * 3600, 9.0 * 3600),
        "wait_ci": (40, 0.83, 0.4 * 3600, 0.9 * 3600),
        "address_review": (25, 0.72, 3.0 * 3600, 6.0 * 3600),
        "merge": (35, 0.81, 1.0 * 3600, 2.0 * 3600),
    }
    for night, coverage in enumerate((0.66, 0.71, 0.74, 0.77, 0.79), start=1):
        by_stage = [
            {
                "stage": stage,
                "n": n,
                "unknown": 3,
                "coverage": round(min(0.95, share + (coverage - 0.76)), 3),
                "median_abs_error_seconds": error,
                "median_width_seconds": width,
            }
            for stage, (n, share, error, width) in stages.items()
        ]
        metrics = {
            "loss": 11000.0 - 500 * night,
            "overall": {
                "stage": "all",
                "n": sum(n for n, *_ in stages.values()),
                "unknown": 12,
                "coverage": coverage,
                "median_abs_error_seconds": (7.5 - 0.3 * night) * 3600,
                "median_width_seconds": 6.0 * 3600,
            },
            "by_stage": by_stage,
            "regime": [],
        }
        db.execute(
            "INSERT INTO estimate_backtests (ran_at, model_version, params, metrics) "
            "VALUES (?, ?, ?, ?)",
            (
                (now - timedelta(days=6 - night)).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f+00:00"
                ),
                MODEL_VERSION,
                json.dumps(
                    {"half_life_weeks": 12.0, "min_samples": 5, "shrinkage": 5.0}
                ),
                json.dumps(metrics),
            ),
        )


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
        autonomous: bool = False,
    ) -> None:
        touched = touched or updated
        document = {
            "version": 2 if autonomous else 1,
            "holder": name,
            "kind": "autonomous" if autonomous else "agent",
            "session_id": None if autonomous else name.split(".")[0],
            "agent_id": None
            if autonomous
            else name.split(".")[1]
            if "." in name
            else None,
            "link": SESSION.format(n=session) if session else None,
            "state": state,
            "started_at": z(updated - timedelta(minutes=20)),
            "updated_at": z(updated),
            "ttl_seconds": ttl,
            "claims": [
                {"kind": "ref", "ref": ref, "touched_at": z(touched)} for ref in refs
            ],
        }
        if autonomous:
            document["run_id"] = name.removeprefix("auto.")
        (claims / f"{name}.json").write_text(json.dumps(document, indent=2))

    day = 8 * 3600
    # Working on a branch whose CI is failing: reads "With an agent", and
    # would be "Fix / re-run CI".
    holder(
        "sess-working", "working", ["devon/auth-docs"], updated=now, ttl=day, session=11
    )
    # One of toolbelt's own autonomous agents (contract v2): reads "Autonomous
    # agent", and would be "Resolve conflicts".
    holder(
        "auto.demo-run",
        "working",
        ["devon/cache-metrics"],
        updated=now,
        ttl=day,
        autonomous=True,
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


def write_lanes(now: datetime) -> None:
    """What `tt ui` would have recorded about the demo branches, so every lane
    of the card timelines (docs/timeline-lanes.md) has something to draw: the
    conflict, CI and review-thread changes it saw, agent runs, claims, Slack
    posts, a queued merge, decisions and a task tracker move. The fake `gh`
    serves the matching PRs; times are relative to now."""

    def ago(**delta: float) -> datetime:
        return now - timedelta(**delta)

    store = StackStore(default_db_path())
    try:
        repo = {r.name: r.id for r in store.repos()}
        task = {(f.repo_id, f.name): f.task_id for f in store.tracked_branch_facts()}

        def app(branch: str) -> tuple[int, str, str]:
            return repo["demo-app"], branch, task[(repo["demo-app"], branch)]

        def search(branch: str) -> tuple[int, str, str]:
            return repo["demo-search"], branch, task[(repo["demo-search"], branch)]

        # Changes of a branch's conflict, CI and open review threads.
        for (repo_id, branch, _), changes in {
            app("devon/auth-models"): [
                ("ci", "running", ago(days=6)),
                ("ci", "success", ago(days=6, hours=-1)),
            ],
            app("devon/auth-api"): [
                ("ci", "running", ago(days=4)),
                ("ci", "failed", ago(days=3, hours=23)),
                ("conflict", "clean", ago(days=4)),
                ("conflict", "conflicts", ago(days=3, hours=6)),
                ("conflict", "clean", ago(days=3)),
                ("ci", "running", ago(days=3, hours=21)),
                ("ci", "success", ago(days=3, hours=20)),
                ("comments", "open", ago(days=1)),
            ],
            app("devon/auth-docs"): [
                ("ci", "running", ago(days=3)),
                ("ci", "failed", ago(days=2, hours=23)),
                ("comments", "open", ago(days=2)),
                ("comments", "none", ago(days=1, hours=12)),
                ("comments", "open", ago(hours=20)),
            ],
            app("devon/cache-metrics"): [
                ("ci", "success", ago(days=2)),
                ("conflict", "clean", ago(days=2)),
                ("conflict", "conflicts", ago(days=1)),
            ],
            search("devon/search-index"): [
                ("ci", "running", ago(days=1)),
            ],
        }.items():
            for dimension, value, at in changes:
                store.record_status_transition(repo_id, branch, dimension, value, at)

        # Autonomous agent runs.
        for (repo_id, branch, task_id), runs in {
            app("devon/auth-docs"): [
                (
                    "fix_ci",
                    "succeeded",
                    ago(days=2, hours=22),
                    14,
                    "Fixed two broken links.",
                ),
                ("fix_ci", "failed", ago(days=1, hours=10), 31, None),
                ("resolve_comments", "stopped", ago(hours=5), 6, None),
            ],
            app("devon/auth-api"): [
                ("resolve_conflicts", "succeeded", ago(days=3, hours=5), 9, "Rebased."),
            ],
            app("devon/cache-metrics"): [
                ("resolve_conflicts", "timed_out", ago(hours=18), 40, None),
                ("resolve_conflicts", "cap_reached", ago(hours=9), 60, None),
            ],
        }.items():
            for trigger, state, started, turns, summary in runs:
                run_id = store.start_agent_run(
                    task_id=task_id,
                    repo_id=repo_id,
                    branch=branch,
                    trigger=trigger,
                    head_sha="demo",
                    model="claude-haiku-5-5",
                    at=started,
                )
                store.finish_agent_run(
                    run_id,
                    state=state,
                    ended_at=started + timedelta(minutes=turns / 2),
                    turns=turns,
                    cost_usd=turns * 0.012,
                    tokens=TokenCounts(),
                    summary=summary,
                )

        # Validate runs and the verdicts on them: auth-sso awaits your review,
        # auth-audit was sent back, auth-lockout has not been validated.
        for (repo_id, branch, task_id), (started, result, summary) in {
            app("devon/auth-sso"): (
                ago(hours=3),
                "pass",
                "Approve: small and tested; one nit on the empty-index case.",
            ),
            app("devon/auth-audit"): (
                ago(days=1, hours=2),
                "fail",
                "Changes required: the audit list is never written anywhere.",
            ),
        }.items():
            run_id = store.start_agent_run(
                task_id=task_id,
                repo_id=repo_id,
                branch=branch,
                trigger="validate",
                head_sha="demo",
                model="claude-haiku-5-5",
                at=started,
            )
            store.finish_agent_run(
                run_id,
                state="succeeded",
                ended_at=started + timedelta(minutes=6),
                turns=22,
                cost_usd=0.21,
                tokens=TokenCounts(),
                summary=summary,
                result=result,
            )
        repo_id, branch, task_id = app("devon/auth-audit")
        store.add_review_verdict(
            task_id=task_id,
            repo_id=repo_id,
            branch=branch,
            head_sha="demo",
            verdict="changes_requested",
            note="Write AUDITED events to the audit log, not only list them.",
            at=ago(days=1),
        )

        # Claims, as the claims feed records them while a holder holds one.
        for (_, _, task_id), claims in {
            app("devon/auth-models"): [
                (
                    "sess-a",
                    "agent",
                    "worktree",
                    "working",
                    ago(days=8),
                    ago(days=7, hours=20),
                ),
                (
                    "sess-b",
                    "agent",
                    "ref",
                    "working",
                    ago(days=7),
                    ago(days=6, hours=22),
                ),
            ],
            app("devon/auth-api"): [
                (
                    "sess-c",
                    "agent",
                    "worktree",
                    "working",
                    ago(days=5),
                    ago(days=4, hours=23),
                ),
                (
                    "sess-c",
                    "agent",
                    "worktree",
                    "idle",
                    ago(days=4, hours=23),
                    ago(days=4, hours=22),
                ),
                (
                    "sess-d",
                    "human",
                    "ref",
                    "working",
                    ago(days=2),
                    ago(days=1, hours=20),
                ),
            ],
            app("devon/cache-metrics"): [
                (
                    "auto.earlier",
                    "autonomous",
                    "worktree",
                    "working",
                    ago(hours=18),
                    ago(hours=17, minutes=15),
                ),
            ],
        }.items():
            for holder, kind, claim_kind, state, first, last in claims:
                store.record_claim_seen(
                    holder=holder,
                    session_id=holder,
                    agent_id=None,
                    holder_kind=kind,
                    claim_kind=claim_kind,
                    target="/demo/worktree" if claim_kind == "worktree" else "demo-ref",
                    task_id=task_id,
                    touched_at=first,
                    seen_at=last,
                    max_gap=timedelta(minutes=1),
                    state=state,
                )

        # Slack posts of PRs (by repo slug and number, as toolbelt records them).
        posts = [
            ("devon/demo-app", 102, "post", ago(days=3, hours=20), "Sam"),
            ("devon/demo-app", 102, "bump", ago(days=2), "Sam"),
            ("devon/demo-app", 102, "reassign", ago(days=1, hours=2), "Lee"),
            ("devon/demo-app", 104, "post", ago(days=2, hours=1), "Lee"),
            ("devon/demo-search", 57, "post", ago(hours=6), "Lee"),
        ]
        store.save_slack_posts(
            [
                SlackPostRow(
                    channel="C0DEMO",
                    ts=f"{int(at.timestamp())}.{number:06d}",
                    thread_ts=f"{int(at.timestamp())}.{number:06d}",
                    repo=slug,
                    pr_number=number,
                    kind=kind,
                    posted_at=at,
                    assignee_id=None,
                    assignee_name=name,
                )
                for slug, number, kind, at, name in posts
            ]
        )

        # A queued merge (approved, CI still running), a decision an agent is
        # waiting on and one that was answered, and a tracker move.
        repo_id, branch, task_id = search("devon/search-cache")
        store.add_to_merge_queue(
            MergeQueueRow(repo_id, branch, 59, f"{59:07x}", ago(hours=2))
        )
        repo_id, branch, task_id = app("devon/auth-docs")
        for index, (status, created, answered) in enumerate(
            [
                ("pending", ago(hours=4), None),
                ("answered", ago(days=2, hours=3), ago(days=2, hours=2)),
            ]
        ):
            decision = Decision(
                id=f"demo-decision-{index}",
                task_id=task_id,
                repo_id=repo_id,
                branch=branch,
                proposer="autonomous",
                session_id=None,
                run_id=None,
                title="Rewrite the broken links or drop them?",
                points=(),
                status="pending",
                created_at=created.isoformat(),
            )
            store.add_decision(decision)
            if status == "answered" and answered:
                store.answer_decision(decision.id, (), answered)
        repo_id, branch, _ = app("devon/auth-api")
        store.record_task_status_move(
            repo_id, branch, "in_review", 102, ago(days=3, hours=20)
        )
    finally:
        store.close()


#: What the demo agents call: (tool, detail, typical ms).
_CALLS = [
    ("Bash", "git status", 400),
    ("Bash", "git diff --stat", 600),
    ("Bash", "uv run pytest tests/test_cache.py -q", 24_000),
    ("Bash", "pnpm --dir web test --run", 18_000),
    ("Bash", "uv run ruff check src", 1_500),
    ("Bash", "rg -n 'cache' src | head -20", 300),
    ("Bash", "gh pr view 101 --json state", 900),
    ("Bash", "cd /demo/wt/demo-app/auth-api && make lint", 6_000),
    ("Bash", "curl -s localhost:8771/api/snapshot | head -c 200", 250),
    ("Read", "/demo/wt/demo-app/auth-api/src/auth.py", 40),
    ("Read", "/demo/wt/demo-app/auth-api/README.md", 30),
    ("Grep", "def login", 120),
    ("Glob", "src/**/*.py", 60),
    ("Edit", "/demo/wt/demo-app/auth-api/src/auth.py", 80),
    ("Write", "/demo/wt/demo-app/auth-api/tests/test_auth.py", 70),
    ("WebFetch", "https://example.com/docs", 1_800),
    ("mcp__demo__lookup", "", 700),
]


def write_activity(home: Path, now: datetime) -> None:
    """Chunk files in the plugin's shape (docs/agent-activity.md), as many
    turns as a few busy weeks: `tt ui` reads them when it starts, so the
    aggregate view's agent activity has something to show."""
    rng = random.Random(20261012)
    out = home / "activity"
    out.mkdir(parents=True, exist_ok=True)

    def z(time: datetime) -> str:
        return time.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    for day in range(21):
        if (now - timedelta(days=day)).weekday() >= 5 and rng.random() < 0.8:
            continue  # mostly weekdays
        for turn in range(rng.randint(2, 7)):
            started = (now - timedelta(days=day)).replace(
                hour=rng.randint(8, 19),
                minute=rng.randint(0, 59),
                second=0,
                microsecond=0,
            ) - timedelta(minutes=rng.randint(0, 59))
            if started > now:
                continue
            clock = started
            calls = []
            tool = delegated = blocked = 0
            for _ in range(rng.randint(5, 30)):
                clock += timedelta(
                    milliseconds=rng.randint(2_000, 25_000)
                )  # the model thinks
                name, detail, typical = rng.choice(_CALLS)
                ms = max(int(typical * rng.uniform(0.4, 2.0)), 10)
                waits_on_subagent = rng.random() < 0.06
                if waits_on_subagent:
                    name, detail, ms = (
                        "Task",
                        "explore the cache layer",
                        rng.randint(60_000, 240_000),
                    )
                stuck = (
                    20_000 if name == "Bash" and rng.random() < 0.05 else 0
                )  # a prompt
                calls.append(
                    {
                        "tool": name,
                        "detail": detail,
                        "started_at": z(clock),
                        "duration_ms": ms + stuck,
                        "blocked_ms": stuck,
                        "ok": rng.random() > 0.06,
                        "unended": False,
                        "worktree": None,
                        "background": False,
                        "delegated": waits_on_subagent,
                    }
                )
                clock += timedelta(milliseconds=ms + stuck)
                if waits_on_subagent:
                    delegated += ms
                else:
                    tool += ms
                blocked += stuck
            wall = int((clock - started).total_seconds() * 1000) + rng.randint(
                1_000, 20_000
            )
            turn_id = f"demo-{day:02d}-{turn}"
            document = {
                "version": 1,
                "holder": "sess-demo",
                "session_id": "sess-demo",
                "agent_id": None,
                "turn_id": turn_id,
                "seq": 0,
                "final": True,
                "started_at": z(started),
                "ended_at": z(started + timedelta(milliseconds=wall)),
                "reason": "answer",
                "totals": {
                    "wall_ms": wall,
                    "tool_ms": tool,
                    "delegated_ms": delegated,
                    "blocked_ms": blocked,
                    "model_ms": wall - tool - delegated - blocked,
                },
                "calls": calls,
            }
            (out / f"sess-demo.{turn_id}.0.json").write_text(json.dumps(document))


def write_sibling_commits(repo: Path) -> None:
    """Real commits for two tasks of demo-search that both change the same line
    of one file, so their heads conflict (the sibling-conflict chips of docs/
    sibling-conflicts.md). The other demo branches have no commits: their heads
    are not in the repo, which the cards show as unknown."""
    identity = ("-c", "user.name=Demo", "-c", "user.email=demo@example.com")
    weights = repo / "search" / "weights.py"
    weights.parent.mkdir(exist_ok=True)

    def commit(message: str, weight: int) -> None:
        weights.write_text(
            "".join(f"# weight {n}\n" for n in range(1, 6))
            + f"TITLE_WEIGHT = {weight}\n"
            + "".join(f"# weight {n}\n" for n in range(7, 12))
        )
        _git(repo, "add", "-A")
        _git(repo, *identity, "commit", "-q", "-m", message)

    commit("Search weights", 2)
    for branch, weight in (("devon/search-index", 3), ("devon/search-cache", 5)):
        _git(repo, "checkout", "-q", "-b", branch, "main")
        commit(f"Weigh titles {weight}", weight)
        _git(repo, "checkout", "-q", "main")


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
    write_sibling_commits(repos / "demo-search")
    (dest / "config").mkdir(exist_ok=True)
    write_branches(dest)

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
                            datetime.now(UTC) - timedelta(days=30),
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
    now = datetime.now(UTC)
    for (_, branch), days in started.items():
        when = (now - timedelta(days=days)).isoformat()
        db.execute("UPDATE branches SET started_at = ? WHERE name = ?", (when, branch))
    write_history(dest, db, now)
    write_estimates(db, now)
    db.commit()
    db.close()
    write_lanes(now)
    write_claims(Path(os.environ["TOOLBELT_HOME"]), now)
    write_activity(Path(os.environ["TOOLBELT_HOME"]), now)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--deep", type=int, default=0, metavar="N")
    args = parser.parse_args()
    seed(args.dest.resolve(), args.deep)


if __name__ == "__main__":
    main()
