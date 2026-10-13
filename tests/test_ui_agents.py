"""The Agents view's server side: `/api/agents` and the on/off switch."""

from datetime import timedelta

from fastapi.testclient import TestClient
from test_dispatcher import NOW, PrSnapshot, Rig, holder, rig, status

from toolbelt.autonomous_agents.limits import AGENTS_ENABLED_SETTING
from toolbelt.git.stack.store import (
    RepoIdentity,
    StackStore,
    TokenCounts,
    default_db_path,
)
from toolbelt.ui import api_models as api
from toolbelt.ui.app import create_app


def client_for(r: Rig) -> TestClient:
    app = create_app(
        tracker=lambda: None, wall_clock=lambda: NOW, dispatcher=r.dispatcher
    )
    return TestClient(app)


def set_enabled(value: bool) -> None:
    store = StackStore(default_db_path())
    store.set_setting(AGENTS_ENABLED_SETTING, "true" if value else "false")
    store.close()


def test_agents_are_off_until_switched_on():
    r = rig(a=PrSnapshot(status(), None))
    r.enabled = False
    r.tick()
    client = client_for(r)

    body = api.Agents.model_validate(client.get("/api/agents").json())

    assert (body.enabled, body.launching, body.error) == (False, False, None)
    assert body.dry_run is True
    assert [b.code for b in body.blockers] == ["disabled"]
    assert body.decisions == []
    assert body.limits.run_usd == 0.25


def test_decisions_list_tasks_an_agent_may_take_first_then_those_it_may_not():
    r = rig(
        a=PrSnapshot(status(), None),
        b=PrSnapshot(status(), None),
    )
    r.holders["b"] = (holder("agent", "idle"),)
    r.tick()
    set_enabled(True)

    body = api.Agents.model_validate(client_for(r).get("/api/agents").json())

    assert body.enabled and body.ticked_at is not None
    assert [
        (d.branch, d.trigger, d.reason and d.reason.code) for d in body.decisions
    ] == [
        ("a", "fix_ci", None),
        ("b", "fix_ci", "claimed"),
    ]
    assert body.decisions[0].id == "1:a"


def test_screen_and_sign_in_are_reported_with_the_blockers():
    r = rig(a=PrSnapshot(status(), None))
    r.signed_in = False
    r.tick()

    body = api.Agents.model_validate(client_for(r).get("/api/agents").json())

    assert body.signed_in is False
    assert (body.screen.awake, body.screen.locked) == (True, False)
    assert [b.code for b in body.blockers] == ["auth"]


def test_runs_and_spend_against_the_limits_come_from_the_run_records():
    r = rig(a=PrSnapshot(status(), None))
    store = StackStore(default_db_path())
    repo_id = store.ensure_repo(
        RepoIdentity(
            git_common_dir=default_db_path().parent / "x" / ".git",
            path=default_db_path().parent / "x",
            remote_url=None,
        )
    )
    run_id = store.start_agent_run(
        task_id="id-a",
        repo_id=repo_id,
        branch="a",
        trigger="fix_ci",
        head_sha="abc",
        model="claude-haiku-5-5",
        at=NOW - timedelta(hours=2),
    )
    store.finish_agent_run(
        run_id,
        state="succeeded",
        ended_at=NOW - timedelta(hours=2) + timedelta(minutes=3),
        turns=7,
        cost_usd=0.12,
        tokens=TokenCounts(input=1, output=2),
        summary="Fixed the lint error.",
    )
    store.close()
    r.tick()

    body = api.Agents.model_validate(client_for(r).get("/api/agents").json())

    assert body.spend.last_24_hours_usd == 0.12
    assert body.spend.this_month_usd == 0.12
    (run,) = body.runs
    assert (run.state, run.turns, run.cost_usd) == ("succeeded", 7, 0.12)
    assert (run.branch, run.trigger, run.summary) == (
        "a",
        "fix_ci",
        "Fixed the lint error.",
    )


def test_the_switch_is_stored_and_read_back():
    r = rig(a=PrSnapshot(status(), None))
    client = client_for(r)

    assert client.get("/api/agents").json()["enabled"] is False
    assert client.post("/api/agents/enabled", json={"enabled": True}).json() == {
        "enabled": True
    }
    assert client.get("/api/agents").json()["enabled"] is True
    client.post("/api/agents/enabled", json={"enabled": False})
    assert client.get("/api/agents").json()["enabled"] is False


def test_a_bad_switch_body_is_rejected():
    client = client_for(rig(a=PrSnapshot(status(), None)))

    assert (
        client.post("/api/agents/enabled", json={"enabled": "maybe"}).status_code == 422
    )
    assert client.post("/api/agents/enabled", json={}).status_code == 422


def test_starting_runs_is_a_second_switch_that_is_off_until_turned_on():
    client = client_for(rig(a=PrSnapshot(status(), None)))

    assert client.get("/api/agents").json()["launching"] is False
    assert client.post("/api/agents/launching", json={"launching": True}).json() == {
        "launching": True
    }
    assert client.get("/api/agents").json()["launching"] is True
    assert client.post("/api/agents/launching", json={}).status_code == 422


def test_a_working_run_can_be_stopped_and_an_unknown_one_is_a_404():
    app = create_app(
        tracker=lambda: None,
        wall_clock=lambda: NOW,
        dispatcher=rig(a=PrSnapshot(status(), None)).dispatcher,
    )
    client = TestClient(app)

    class Task:
        cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    task = Task()
    app.state.agent_runner.running["r1"] = task

    assert client.post("/api/agents/runs/r1/stop").json() == {"stopping": True}
    assert task.cancelled is True
    assert client.post("/api/agents/runs/nope/stop").status_code == 404
