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
    assert len(everything) == len(prs) + 7
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
    assert sum(result.throughput.merged) >= 25
