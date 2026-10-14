"""The spend view's server side: the feed that keeps the ledger current and the
`/api/spend` endpoint. Transcripts and the database are temp files."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from test_token_spend import claim, row, write_transcript

from toolbelt.git.stack.store import StackStore, default_db_path
from toolbelt.ui import api_models as api
from toolbelt.ui.app import create_app
from toolbelt.ui.spend_feed import SpendFeed

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def feed(tmp_path: Path, projects: Path | None = None) -> SpendFeed:
    return SpendFeed(
        db_path=default_db_path,
        projects=lambda: projects or tmp_path / "projects",
        wall_clock=lambda: NOW,
        interval=60,
    )


def test_before_the_first_pass_the_view_is_empty_and_says_nothing_was_read(tmp_path):
    spend = feed(tmp_path).spend(7)

    assert spend.ingested_at is None
    assert [w.calls for w in spend.windows] == [0, 0, 0, 0]
    assert spend.by_model == [] and spend.by_task == []


def test_a_pass_makes_the_transcripts_calls_show_up_in_the_view(tmp_path):
    projects = tmp_path / "projects"
    write_transcript(
        projects / "proj" / "s1.jsonl",
        row("m1", at=NOW - timedelta(hours=1)),
        row("m2", at=NOW - timedelta(hours=2), model="claude-new-1"),
    )
    spend_feed = feed(tmp_path, projects)

    spend_feed.ingest()
    spend = spend_feed.spend(7)

    assert spend.ingested_at == NOW.isoformat()
    assert spend.windows[0].label == "Last 24 hours"
    assert spend.windows[0].calls == 2
    assert [m.model for m in spend.by_model] == ["claude-sonnet-5-5", "claude-new-1"]
    assert [(u.model, u.calls) for u in spend.unpriced_models] == [("claude-new-1", 1)]
    assert spend.by_task[0].task_id is None


def test_claims_recorded_before_the_calls_attribute_them_to_the_task(tmp_path):
    projects = tmp_path / "projects"
    write_transcript(
        projects / "proj" / "s1.jsonl", row("m1", at=NOW - timedelta(minutes=30))
    )
    store = StackStore(default_db_path())
    claim(store, "task-a", NOW - timedelta(hours=1), NOW)
    store.close()
    spend_feed = feed(tmp_path, projects)

    spend_feed.ingest()
    spend = spend_feed.spend(1)

    assert [(t.task_id, t.calls) for t in spend.by_task] == [("task-a", 1.0)]
    assert spend.claims_since == (NOW - timedelta(hours=1)).isoformat()


def test_the_run_loop_reads_the_transcripts_again_each_interval(tmp_path):
    projects = tmp_path / "projects"
    transcript = projects / "proj" / "s1.jsonl"
    write_transcript(transcript, row("m1", at=NOW - timedelta(hours=1)))
    spend_feed = feed(tmp_path, projects)

    async def scenario():
        ticks = asyncio.Event()

        async def sleep(_seconds: float) -> None:
            await ticks.wait()
            ticks.clear()

        spend_feed._sleep = sleep
        task = asyncio.create_task(spend_feed.run())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if spend_feed.ingested_at:
                break
        assert spend_feed.spend(1).windows[0].calls == 1
        with transcript.open("a") as handle:
            handle.write(row("m2", at=NOW - timedelta(minutes=5)) + "\n")
        ticks.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if spend_feed.spend(1).windows[0].calls == 2:
                break
        assert spend_feed.spend(1).windows[0].calls == 2
        task.cancel()

    asyncio.run(scenario())


def test_the_endpoint_serves_the_spend_and_checks_the_window(tmp_path):
    app = create_app(
        tracker=lambda: None,
        wall_clock=lambda: NOW,
        transcripts_dir=lambda: tmp_path / "projects",
    )
    write_transcript(
        tmp_path / "projects" / "proj" / "s1.jsonl",
        row("m1", at=NOW - timedelta(hours=1)),
    )
    app.state.spend_feed.ingest()
    client = TestClient(app)

    body = client.get("/api/spend?days=30").json()
    parsed = api.Spend.model_validate(body)

    assert parsed.breakdown_days == 30
    assert parsed.windows[0].calls == 1
    assert json.loads(client.get("/api/spend").text)["breakdown_days"] == 7
    assert client.get("/api/spend?days=3").status_code == 400
