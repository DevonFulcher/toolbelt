"""A believable ~12 weeks of PR history for the demo's aggregate view.

`history(now, rng)` returns ``(prs, tasks)``:

* ``prs``: PR specs in the format the fake `gh` (tools/ui-demo/gh) turns into
  GraphQL search nodes. Their events are ``[kind, iso time, ...]``: ``ready``,
  ``draft``, ``request <reviewer>``, ``review <reviewer> <STATE>``,
  ``commit <oid> <CI state|None> <checks done iso|None>``, ``base <from> <to>``,
  ``merged``, ``closed``.
* ``tasks``: the archived task facts (what the stack DB's ``removed_branches``
  holds) of the branches that were cleaned up after those PRs, plus a few that
  never got a PR. Only PRs from the last ``RECORDED_WEEKS`` weeks have one:
  older ones show "no task start recorded", as a real database would. About
  two thirds carry their PR's identity (``pr``: number and node id, as the CLI
  records it); the rest do not, so the view falls back to the branch name for
  those, and one branch name is used by two tasks (each linked by id).

Lead times vary on purpose: quick and slow reviews, changes requested,
failing CI, drafts, PRs closed unmerged and two stacked pairs (the child is
approved first, then its base moves to main when its parent merges).
"""

import random
from datetime import datetime, timedelta

HISTORY_WEEKS = 12
RECORDED_WEEKS = 9

_TOPICS = [
    "Retry policy",
    "Rate limiter",
    "Audit log",
    "Config loader",
    "Search facets",
    "Pagination",
    "Webhook signing",
    "CSV export",
    "Feature flags",
    "Session expiry",
    "Metrics tags",
    "Index warmup",
    "Dark mode",
    "Error pages",
    "Batch import",
    "Cache headers",
    "Health checks",
    "Job queue",
    "Slug rules",
    "Retry budget",
    "Billing sync",
    "Permissions",
    "Locale fallback",
    "Image resize",
    "Mail digest",
    "Query planner",
    "Soft delete",
    "API tokens",
    "Tracing",
    "Schema checks",
    "Quota alerts",
    "Export jobs",
    "Lockfile audit",
    "Token refresh",
    "Cursor paging",
    "Backfill tool",
    "Rollup views",
    "Idle timeout",
    "Bulk edit",
    "Shadow reads",
]


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def node_id(number: int) -> str:
    """The fake GitHub node id of PR ``number`` (shared with the fake `gh`)."""
    return f"PR_kwDEMO{number:06d}"


def history(now: datetime, rng: random.Random) -> tuple[list[dict], list[dict]]:
    prs: list[dict] = []
    tasks: list[dict] = []
    number = 200
    order = rng.sample(_TOPICS, len(_TOPICS))
    recorded_since = now - timedelta(weeks=RECORDED_WEEKS)

    def hours(low: float, high: float) -> timedelta:
        return timedelta(hours=rng.uniform(low, high))

    def make(created: datetime, *, repo: str, base: str = "main") -> dict:
        nonlocal number
        number += 1
        # Past the list, a topic comes round again with a suffix (unique).
        round_, index = divmod(number - 201, len(order))
        topic = order[index] + (f" {round_ + 1}" if round_ else "")
        return {
            "number": number,
            "node_id": node_id(number),
            "title": topic,
            "repo": repo,
            "head": f"devon/{topic.lower().replace(' ', '-')}",
            "base": base,
            "created": created,
            "events": [],
        }

    def commit(spec: dict, when: datetime, ci: str | None):
        """A push whose checks finished 20 minutes to an hour and a half
        later (so "waiting on CI" is a thin sliver, as it is for real)."""
        oid = f"{spec['number']:04x}{len(spec['events']):03x}"
        done = iso(when + hours(0.3, 1.5)) if ci else None
        spec["events"].append(["commit", when, oid, ci, done])

    def journey(spec: dict, *, outcome: str, draft: bool, pace: float) -> datetime:
        """Fills ``spec`` with a PR's life and returns when it ended."""
        t = spec["created"]
        commit(spec, t - timedelta(minutes=10), "SUCCESS")
        if draft:
            spec["is_draft_at_open"] = True
            spec["events"].append(["draft", t])
            t += hours(2, 26) * pace
            spec["events"].append(["ready", t])
        reviewer = rng.choice(["sam", "lee", "kim"])
        t += hours(0.1, 6) * pace
        if rng.random() < 0.12:  # forgot to ask: requested much later
            t += hours(20, 60)
        spec["events"].append(["request", t, reviewer])
        if outcome == "closed":
            t += hours(20, 90)
            if rng.random() < 0.5:
                spec["events"].append(["review", t, reviewer, "COMMENTED"])
                t += hours(2, 20)
            spec["events"].append(["closed", t])
            spec["closed"] = t
            return t
        t += hours(1.5, 40) * pace
        if rng.random() < 0.2:
            spec["events"].append(["review", t, reviewer, "COMMENTED"])
            t += hours(1, 8)
        if rng.random() < 0.4:
            spec["events"].append(["review", t, reviewer, "CHANGES_REQUESTED"])
            t += hours(1, 9)
            if rng.random() < 0.4:
                commit(spec, t, "FAILURE")
                t += hours(0.5, 3)
            commit(spec, t, "SUCCESS")
            t += hours(0.5, 4)
            spec["events"].append(["request", t, reviewer])
            t += hours(1, 24) * pace
        spec["events"].append(["review", t, reviewer, "APPROVED"])
        t += hours(0.3, 20) * (1.5 if rng.random() < 0.2 else 1)
        spec["events"].append(["merged", t])
        spec["merged"] = t
        return t

    for week in range(HISTORY_WEEKS):
        start = now - timedelta(weeks=HISTORY_WEEKS - week)
        for _ in range(rng.choice([2, 2, 3, 3, 4])):
            created = start + timedelta(
                days=rng.uniform(0, 6.2), hours=rng.uniform(-3, 3)
            )
            if created > now - timedelta(days=0.5):
                continue
            spec = make(
                created, repo=rng.choice(["demo-app", "demo-app", "demo-search"])
            )
            outcome = "closed" if rng.random() < 0.1 else "merged"
            ended = journey(
                spec,
                outcome=outcome,
                draft=rng.random() < 0.35,
                pace=rng.choice([1, 1.5, 2, 3, 4]),
            )
            if ended > now - timedelta(hours=2):
                continue  # not finished yet: the live scenarios cover open PRs
            spec["state"] = "MERGED" if outcome == "merged" else "CLOSED"
            prs.append(spec)

    # One branch name used for two tasks: the later PR re-uses the earlier
    # one's head. Recorded ids keep them apart.
    recorded = [
        spec
        for spec in prs
        if spec["created"] >= recorded_since and spec["repo"] == "demo-app"
    ]
    reused_head = recorded[0]["head"]
    recorded[-1]["head"] = reused_head
    always_linked = {recorded[0]["number"], recorded[-1]["number"]}

    # Two stacked pairs: the child is approved while its parent still waits.
    for repo, weeks_ago in (("demo-app", 5.5), ("demo-search", 2.5)):
        parent = make(now - timedelta(weeks=weeks_ago), repo=repo)
        t = parent["created"]
        commit(parent, t - timedelta(minutes=10), "SUCCESS")
        t += hours(0.5, 1)
        parent["events"].append(["request", t, "sam"])
        child = make(t + hours(1, 3), repo=repo, base=parent["head"])
        commit(child, child["created"] - timedelta(minutes=5), "SUCCESS")
        c = child["created"] + hours(0.5, 1)
        child["events"].append(["request", c, "lee"])
        c += hours(3, 10)
        child["events"].append(["review", c, "lee", "APPROVED"])
        t = max(t, c) + hours(8, 30)
        parent["events"].append(["review", t, "sam", "APPROVED"])
        t += hours(0.5, 2)
        parent["events"].append(["merged", t])
        parent["merged"] = t
        parent["state"] = "MERGED"
        child["events"].append(["base", t, parent["head"], "main"])
        child["base"] = "main"
        t += hours(1, 5)
        child["events"].append(["merged", t])
        child["merged"] = t
        child["state"] = "MERGED"
        prs += [parent, child]

    for spec in prs:
        ended = spec.get("merged") or spec["closed"]
        if spec["created"] >= recorded_since:
            linked = spec["number"] in always_linked or rng.random() < 0.67
            tasks.append(
                {
                    "repo": spec["repo"],
                    "branch": spec["head"],
                    "started": spec["created"]
                    - timedelta(hours=rng.choice([1, 2, 4, 6, 10, 20, 30, 50])),
                    "removed": ended + timedelta(hours=rng.uniform(0.2, 30)),
                    "landed": "merged" in spec,
                    "pr": {"number": spec["number"], "node_id": spec["node_id"]}
                    if linked
                    else None,
                }
            )
    # Tasks that never got a PR: started, then dropped.
    for repo, ago, lived in (
        ("demo-app", 38, 5),
        ("demo-search", 21, 9),
        ("demo-app", 12, 2),
    ):
        tasks.append(
            {
                "repo": repo,
                "branch": f"devon/spike-{ago}",
                "started": now - timedelta(days=ago),
                "removed": now - timedelta(days=ago - lived),
                "landed": False,
                "pr": None,
            }
        )

    for spec in prs:
        spec["created"] = iso(spec["created"])
        spec["draft"] = False
        spec["events"] = [
            [event[0], iso(event[1]), *event[2:]]
            for event in sorted(spec["events"], key=lambda event: event[1])
        ]
        for key in ("merged", "closed"):
            if key in spec:
                spec[key] = iso(spec[key])
        spec.pop("is_draft_at_open", None)
    return prs, tasks
