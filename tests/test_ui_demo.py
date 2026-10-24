"""The demo's aggregate history (tools/ui-demo): the generated PRs and the fake
`gh` that serves them must stay in step with what the aggregate view parses,
or the demo silently shows an empty view."""

import importlib.util
import json
import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from toolbelt.ui import aggregate
from toolbelt.ui.aggregate_feed import PR_QUERY, parse_page
from toolbelt.ui.pr_fetch import PR_QUERY as LOOKUP_QUERY
from toolbelt.ui.pr_fetch import parse_snapshot

DEMO = Path(__file__).parent.parent / "tools" / "ui-demo"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _load_history():
    spec = importlib.util.spec_from_file_location(
        "ui_demo_history", DEMO / "history.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_history_spans_twelve_weeks_with_varied_finished_prs():
    module = _load_history()

    prs, tasks = module.history(NOW, random.Random(1))

    assert 25 <= len(prs) <= 60
    assert {pr["state"] for pr in prs} == {"MERGED", "CLOSED"}
    created = sorted(pr["created"] for pr in prs)
    assert created[0] < (NOW - timedelta(weeks=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for pr in prs:
        ended = pr.get("merged") or pr.get("closed")
        assert ended and ended <= NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert [e[1] for e in pr["events"]] == sorted(e[1] for e in pr["events"])
    # Some PRs have no recorded start (older than the archive), some do.
    recorded = {task["branch"] for task in tasks}
    with_start = [pr for pr in prs if pr["head"] in recorded]
    assert 0 < len(with_start) < len(prs)
    # A few tasks never got a PR; a stacked child's base moves to main.
    assert any(task["branch"].startswith("devon/spike-") for task in tasks)
    assert any(
        any(e[0] == "base" for e in pr["events"]) and pr["base"] == "main" for pr in prs
    )


def test_the_fake_gh_answers_the_aggregate_search_in_the_shape_the_view_parses(
    tmp_path: Path,
):
    module = _load_history()
    prs, _ = module.history(NOW, random.Random(1))
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "history.json").write_text(json.dumps(prs))
    env = {"TOOLBELT_HOME": str(home), "PATH": "/usr/bin:/bin"}

    def ask(search: str) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(DEMO / "gh"),
                "api",
                "graphql",
                "-f",
                f"query={PR_QUERY}",
                "-f",
                f"search={search}",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return json.loads(result.stdout)

    everything, more, cursor, hidden = parse_page(
        ask("is:pr author:@me updated:>=2026-07-01")
    )
    open_only, *_ = parse_page(ask("is:pr is:open author:@me archived:false"))

    assert (more, cursor, hidden) == (False, None, 0)
    # The history plus the live scenarios' PRs.
    assert len(everything) == len(prs) + 8
    assert {r.facts.state for r in open_only} == {"OPEN"}
    assert {r.repo for r in everything} == {"devon/demo-app", "devon/demo-search"}
    merged = [r for r in everything if r.facts.state == "MERGED"]
    assert all(r.facts.merged_at for r in merged)
    # A stacked child's base change is parsed and the CI completion times too.
    assert any(r.base_changes for r in everything)
    assert any(i.ci_done for r in everything for i in r.facts.items)
    # And the whole thing computes.
    result = aggregate.compute(
        everything,
        [],
        now=NOW,
        weeks=12,
        archive_since=NOW - timedelta(weeks=9),
        fetched_at=NOW,
    )
    assert sum(1 for m in result.prs if m.state == "MERGED") >= 25


def _fake_gh(*args: str, home: Path) -> str:
    return subprocess.run(
        [sys.executable, str(DEMO / "gh"), *args],
        capture_output=True,
        text=True,
        env={"TOOLBELT_HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=True,
    ).stdout


def test_the_fake_gh_gives_the_token_the_ui_asks_for(tmp_path: Path):
    assert _fake_gh("auth", "token", home=tmp_path).strip() == "demo-token"


def test_every_demo_branch_looks_up_in_the_shape_the_status_lookup_parses(
    tmp_path: Path,
):
    # The per-PR lookup the cards use (not the aggregate search): a field the
    # query gained and the canned PRs lack would fail every demo card.
    scenarios = [
        line.split('"')[1]
        for line in (DEMO / "gh").read_text().splitlines()
        if line.startswith('    "devon/') and line.rstrip().endswith("pr(")
    ]
    assert len(scenarios) == 8
    for branch in scenarios:
        payload = json.loads(
            _fake_gh(
                "api",
                "graphql",
                "-f",
                f"query={LOOKUP_QUERY}",
                "-f",
                f"branch={branch}",
                home=tmp_path,
            )
        )
        snapshot = parse_snapshot(payload)
        assert snapshot.facts is not None, branch


def test_the_seeded_demo_gives_every_lane_something_to_draw(tmp_path: Path):
    """A lane added to the registry needs a row in ``write_lanes`` (seed.py),
    or the demo cannot show it."""
    from toolbelt.git.stack.store import StackStore, default_db_path
    from toolbelt.ui.lane_history import build_lanes
    from toolbelt.ui.timeline_lanes import LANE_SOURCES
    from toolbelt.ui.untracked_prs import repo_slug

    spec = importlib.util.spec_from_file_location("ui_demo_seed", DEMO / "seed.py")
    assert spec and spec.loader
    seed = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(DEMO))
    try:
        spec.loader.exec_module(seed)
    finally:
        sys.path.remove(str(DEMO))
    seed.seed(tmp_path, 0)

    store = StackStore(default_db_path())
    try:
        slugs = {r.id: repo_slug(r.remote_url) for r in store.repos()}
        lanes = build_lanes(store, datetime.now(UTC), slugs.get)
        drawn = {lane.lane for task in lanes.values() for lane in task}
    finally:
        store.close()

    # "next" is folded from each PR's facts (card_extras), not recorded.
    assert drawn == {source.id for source in LANE_SOURCES} - {"next"}


def test_the_seeded_agent_activity_is_readable_and_adds_up(tmp_path: Path):
    from toolbelt.agent_activity.chunks import parse_chunk
    from toolbelt.agent_activity.shell import CATEGORIES, classify

    spec = importlib.util.spec_from_file_location("ui_demo_seed2", DEMO / "seed.py")
    assert spec and spec.loader
    seed = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(DEMO))
    try:
        spec.loader.exec_module(seed)
    finally:
        sys.path.remove(str(DEMO))
    seed.write_activity(tmp_path, NOW)

    chunks = [
        parse_chunk(p.read_text()) for p in (tmp_path / "activity").glob("*.json")
    ]

    assert len(chunks) > 40
    for chunk in chunks:
        assert chunk.totals is not None
        totals = chunk.totals
        assert (
            totals.tool_ms + totals.delegated_ms + totals.blocked_ms + totals.model_ms
            == totals.wall_ms
        )
        assert totals.model_ms >= 0
        for recorded in chunk.calls:
            assert classify(recorded.tool, recorded.detail).category in CATEGORIES


def test_the_seeded_search_tasks_really_conflict_and_the_others_are_unknown(
    tmp_path: Path,
):
    """The demo's sibling-conflict chips (docs/sibling-conflicts.md) need two
    tasks whose heads conflict in a real repo."""
    from toolbelt.git.stack.store import StackStore, default_db_path
    from toolbelt.ui.sibling_conflicts import compute_repo

    spec = importlib.util.spec_from_file_location("ui_demo_seed3", DEMO / "seed.py")
    assert spec and spec.loader
    seed = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(DEMO))
    try:
        spec.loader.exec_module(seed)
    finally:
        sys.path.remove(str(DEMO))
    seed.seed(tmp_path, 0)

    store = StackStore(default_db_path())
    try:
        repo = next(r for r in store.repos() if r.name == "demo-search")
        parents = store.all_parents(repo.id)
    finally:
        store.close()

    class Memory:
        def __init__(self) -> None:
            self.rows: dict = {}

        def load(self, repo_id):
            return dict(self.rows)

        def save(self, repo_id, pairs):
            self.rows.update(pairs)

        def prune(self, repo_id, heads):
            pass

    heads, conflicts = compute_repo(
        repo.path, repo.id, parents, dict.fromkeys(parents, ""), Memory()
    )

    assert [(c.other, c.state, c.files) for c in conflicts["devon/search-index"]] == [
        ("devon/search-cache", "conflict", ("search/weights.py",)),
    ]
    assert heads["devon/search-ranking"] is None


def test_the_seeded_demo_shows_validate_personal_review_and_publish(tmp_path: Path):
    """One branch awaits its personal review, one was sent back, one was never
    validated; approving pushes to a local bare repo and the fake `gh` opens
    the PR (nothing reaches GitHub)."""
    import os

    from toolbelt.git.stack.store import StackStore, default_db_path
    from toolbelt.ui.personal_review import current_verdict, validated_tasks

    spec = importlib.util.spec_from_file_location("ui_demo_seed4", DEMO / "seed.py")
    assert spec and spec.loader
    seed = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(DEMO))
    try:
        spec.loader.exec_module(seed)
    finally:
        sys.path.remove(str(DEMO))
    seed.seed(tmp_path, 0)

    store = StackStore(default_db_path())
    try:
        task = {f.name: f.task_id for f in store.tracked_branch_facts()}
        waiting = validated_tasks(store)
        sent_back = store.review_verdicts(task_id=task["devon/auth-audit"])
        results = {
            run.branch: run.result
            for run in store.agent_runs()
            if run.trigger == "validate"
        }
    finally:
        store.close()
    assert waiting == {task["devon/auth-sso"]}
    assert results == {"devon/auth-sso": "pass", "devon/auth-audit": "fail"}
    assert [v.verdict for v in sent_back] == ["changes_requested"]
    assert current_verdict(sent_back, "any") is not None
    assert "devon/auth-lockout" in task  # tracked, never validated

    repo = tmp_path / "repos" / "demo-app"
    for branch in (
        "devon/auth-sso",
        "devon/auth-audit",
    ):
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", branch],
            check=True,
            capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "devon/auth-sso"],
        check=True,
        capture_output=True,
    )
    pushed = subprocess.run(
        ["git", "-C", str(tmp_path / "origin" / "demo-app.git"), "branch"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "devon/auth-sso" in pushed

    created = subprocess.run(
        [
            sys.executable,
            str(DEMO / "gh"),
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            "devon/auth-sso",
            "--fill",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ},
    ).stdout.strip()
    assert created == "https://github.com/devon/demo-app/pull/200"
    lookup = subprocess.run(
        [
            sys.executable,
            str(DEMO / "gh"),
            "api",
            "graphql",
            "-f",
            "branch=devon/auth-sso",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ},
    ).stdout
    (found,) = json.loads(lookup)["data"]["repository"]["pullRequests"]["nodes"]
    assert (found["number"], found["state"], found["isDraft"]) == (200, "OPEN", False)


def _demo_records(tmp_path: Path):
    module = _load_history()
    prs, _ = module.history(NOW, random.Random(1))
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "history.json").write_text(json.dumps(prs))
    payload = json.loads(
        _fake_gh(
            "api",
            "graphql",
            "-f",
            f"query={PR_QUERY}",
            "-f",
            "search=is:pr author:@me updated:>=2026-07-01",
            home=home,
        )
    )
    records, *_ = parse_page(payload)
    return records


def test_the_demo_history_is_enough_for_estimates_and_a_backtest(tmp_path: Path):
    """The demo shows estimates and the accuracy section only if its history
    has enough finished PRs per stage, several reviewers, and a backtest that
    predicts most of them."""
    from collections import Counter

    from toolbelt.finish_estimate.backtest import metrics_of, replay
    from toolbelt.finish_estimate.baseline import BaselineModel
    from toolbelt.finish_estimate.working_hours import WorkingHours
    from toolbelt.ui.estimate_feed import stage_rows, timelines_of

    records = _demo_records(tmp_path)
    tasks = [aggregate.Task(pr=record, started=None) for record in records]
    with_pr, resolved, _ = aggregate.pr_lives(tasks, NOW)
    lives = list(zip(with_pr, resolved, strict=True))
    rows = stage_rows(lives, NOW)

    reviews = [row for row in rows if row.stage == "wait_review"]
    assert len(reviews) >= 25
    by_reviewer = Counter(row.reviewer for row in reviews)
    assert {"sam", "lee", "kim"} <= set(by_reviewer)
    assert all(by_reviewer[name] >= 5 for name in ("sam", "lee", "kim"))

    hours = WorkingHours()
    out = replay(
        {"baseline": BaselineModel(hours)},
        timelines_of(lives),
        hours,
        NOW,
        max_tasks=100,
    )
    overall = metrics_of(out["baseline"])
    assert overall.n >= 100
    assert overall.unknown < overall.n * 0.4
    assert overall.coverage is not None and 0.6 <= overall.coverage <= 0.95


def test_the_seeded_demo_has_earlier_backtests_for_the_accuracy_chart(tmp_path: Path):
    from toolbelt.finish_estimate.working_hours import WorkingHours
    from toolbelt.git.stack.store import StackStore, default_db_path
    from toolbelt.ui.estimate_feed import accuracy

    spec = importlib.util.spec_from_file_location("ui_demo_seed3", DEMO / "seed.py")
    assert spec and spec.loader
    seed = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(DEMO))
    try:
        spec.loader.exec_module(seed)
    finally:
        sys.path.remove(str(DEMO))
    seed.seed(tmp_path, 0)

    store = StackStore(default_db_path())
    try:
        track = accuracy(store, WorkingHours())
    finally:
        store.close()

    assert len(track.backtests) >= 3
    assert [c.stage for c in track.champion_changes] == ["all", "wait_review"]
    assert {m.model for m in track.backtests[-1].models} == {
        "baseline",
        "regression",
        "hazard",
        "trees",
    }
    assert track.backtests[-1].ablation
    assert track.current is not None
    assert all(b.overall.coverage is not None for b in track.backtests)
    assert {s.stage for s in track.backtests[-1].by_stage} >= {"wait_review", "merge"}
