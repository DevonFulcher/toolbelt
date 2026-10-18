"""The views pushed to the page: worked out when something changes, published
only when they differ, and in the snapshot for a page that has just opened."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from test_dispatcher import NOW, PrSnapshot, rig, status
from test_ui_card_extras import finished_run, tracked_store

from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.autonomous_agents.runner import AgentRunner
from toolbelt.git.stack.store import default_db_path
from toolbelt.token_spend.transcripts import projects_dir
from toolbelt.ui import api_models as api
from toolbelt.ui.app import create_app
from toolbelt.ui.live_status import EventHub
from toolbelt.ui.pushed_views import PushedViews
from toolbelt.ui.spend_feed import SpendFeed


class Settings:
    enabled = True
    launching = False


def build(r, settings: Settings | None = None, **kwargs):
    settings = settings or Settings()
    hub = EventHub()
    spend = SpendFeed(
        db_path=default_db_path, projects=projects_dir, wall_clock=lambda: NOW
    )
    runner = AgentRunner(
        db_path=default_db_path,
        claims_dir=lambda: default_db_path().parent / "claims",
        limits=AgentLimits(),
        wall_clock=lambda: NOW,
    )
    views = PushedViews(
        hub=hub,
        dispatcher=r.dispatcher,
        runner=runner,
        spend=spend,
        db_path=default_db_path,
        projects=projects_dir,
        enabled=lambda: settings.enabled,
        launching=lambda: settings.launching,
        wall_clock=lambda: NOW,
        **kwargs,
    )
    return views, hub, runner, spend


def drain(queue: asyncio.Queue) -> list[tuple[str, dict]]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_the_card_extras_are_published_when_worked_out_and_again_only_when_they_change():
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    store, repo_id, task_id = tracked_store()
    views, hub, _, _ = build(r)

    async def scenario():
        queue = hub.subscribe()
        await views._refresh("extras")
        await views._refresh("extras")  # nothing changed
        first = drain(queue)
        finished_run(store, repo_id, task_id, 0.02, NOW - timedelta(hours=1))
        await views._refresh("extras")
        return first, drain(queue)

    first, second = asyncio.run(scenario())

    assert [name for name, _ in first] == ["extras"]
    extras = api.CardExtras.model_validate(first[0][1]["extras"])
    assert [b.id for b in extras.branches] == [f"{repo_id}:a"]
    assert extras.branches[0].agent.trigger == "fix_ci"
    assert [name for name, _ in second] == ["extras"]
    assert second[0][1]["extras"]["branches"][0]["spend"]["autonomous_usd"] == 0.02
    assert views.extras is not None


def test_the_agents_view_is_republished_when_a_switch_is_flipped():
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    settings = Settings()
    views, hub, _, _ = build(r, settings)

    async def scenario():
        queue = hub.subscribe()
        await views._refresh("agents")
        settings.launching = True
        await views._refresh("agents")
        return drain(queue)

    events = asyncio.run(scenario())

    assert [name for name, _ in events] == ["agents", "agents"]
    flags = [api.Agents.model_validate(e["agents"]).launching for _, e in events]
    assert flags == [False, True]
    assert [
        d.branch for d in api.Agents.model_validate(events[0][1]["agents"]).decisions
    ] == ["a"]


def test_the_spend_view_is_pushed_for_each_window():
    r = rig(a=PrSnapshot(status(), None))
    views, hub, _, spend = build(r)
    spend.ingest()

    async def scenario():
        queue = hub.subscribe()
        await views._refresh("spend")
        return drain(queue)

    ((name, data),) = asyncio.run(scenario())

    assert name == "spend"
    assert [api.Spend.model_validate(s).breakdown_days for s in data["spend"]] == [
        1,
        7,
        30,
    ]
    assert views.spend and len(views.spend) == 3


def test_changes_before_a_pass_starts_share_it_and_one_during_it_makes_a_second_pass():
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    tracked_store()
    views, _hub, _, _ = build(r)
    passes: list[int] = []
    real = views._refresh

    async def scenario():
        gate = asyncio.Event()

        async def counted(kind: str) -> None:
            passes.append(1)
            if len(passes) == 1:
                await gate.wait()
            await real(kind)

        views._refresh = counted  # type: ignore[method-assign]
        views.notify("extras")
        views.notify("extras")  # before the first pass has started
        await asyncio.sleep(0)
        assert passes == [1]  # in flight, waiting on the gate
        views.notify("extras")  # during it
        gate.set()
        while views._running:
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(passes) == 2


def test_notifying_with_no_event_loop_does_nothing():
    r = rig(a=PrSnapshot(status(), None))
    views, _, _, _ = build(r)

    views.notify("extras", "agents", "spend")

    assert views.extras is None


def test_the_conversation_of_a_working_run_is_pushed_as_it_grows_and_once_more_when_it_ends(
    tmp_path: Path,
):
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    store, repo_id, task_id = tracked_store()
    run_id = store.start_agent_run(
        task_id=task_id,
        repo_id=repo_id,
        branch="a",
        trigger="fix_ci",
        head_sha="abc",
        model="m",
        at=NOW,
        session_id="s-live",
    )
    transcript = projects_dir() / "-wt-a" / "s-live.jsonl"
    transcript.parent.mkdir(parents=True)

    def say(text: str) -> None:
        with transcript.open("a") as out:
            out.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-10-13T17:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": text}],
                        },
                    }
                )
                + "\n"
            )

    views, hub, runner, _ = build(r, conversation_tick=0)

    async def scenario():
        queue = hub.subscribe()
        runner.running[run_id] = None  # type: ignore[assignment]
        watcher = asyncio.create_task(views.watch_conversations())
        say("Looking at CI.")
        await asyncio.sleep(0.05)
        say("Found it.")
        await asyncio.sleep(0.05)
        store.finish_agent_run(
            run_id,
            state="succeeded",
            ended_at=NOW,
            session_id="s-live",
            turns=2,
            cost_usd=0.01,
            tokens=api_tokens(),
        )
        del runner.running[run_id]
        await asyncio.sleep(0.05)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        return drain(queue)

    events = asyncio.run(scenario())

    conversations = [
        api.AgentConversation.model_validate(e["conversation"])
        for name, e in events
        if name == "conversation"
    ]
    assert [len(c.messages) for c in conversations] == [1, 2, 2]
    assert [c.running for c in conversations] == [True, True, False]
    assert conversations[-1].messages[-1].text == "Found it."


def api_tokens():
    from toolbelt.git.stack.store import TokenCounts

    return TokenCounts()


def test_the_snapshot_carries_what_has_been_worked_out_for_a_page_that_just_opened():
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    tracked_store()
    app = create_app(
        tracker=lambda: None, wall_clock=lambda: NOW, dispatcher=r.dispatcher
    )
    client = TestClient(app)

    assert client.get("/api/snapshot").json()["extras"] is None

    views = app.state.views
    asyncio.run(views._refresh("extras"))
    asyncio.run(views._refresh("agents"))
    asyncio.run(views._refresh("spend"))
    body = api.Snapshot.model_validate(client.get("/api/snapshot").json())

    assert body.extras is not None and [
        b.id.split(":")[1] for b in body.extras.branches
    ] == ["a"]
    assert body.agents is not None and body.agents.enabled is False
    assert [s.breakdown_days for s in body.spend] == [1, 7, 30]


def test_changes_to_the_agents_runs_spend_and_stack_all_reach_the_views():
    r = rig(a=PrSnapshot(status(), None))
    app = create_app(
        tracker=lambda: None, wall_clock=lambda: NOW, dispatcher=r.dispatcher
    )

    assert app.state.dispatcher.on_tick is not None
    assert app.state.agent_runner.on_change is not None
    assert app.state.spend_feed.on_ingest is not None
    assert app.state.structure_watcher.on_change is not None
