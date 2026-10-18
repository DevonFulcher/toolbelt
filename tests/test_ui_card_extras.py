"""The task card's agent and spend parts: `/api/card-extras`."""

from datetime import timedelta

from fastapi.testclient import TestClient
from test_dispatcher import NOW, PrSnapshot, holder, rig, status
from test_token_spend import call, claim

from toolbelt.git.stack.store import (
    RepoIdentity,
    StackStore,
    TokenCounts,
    default_db_path,
)
from toolbelt.ui import api_models as api
from toolbelt.ui.app import create_app
from toolbelt.ui.card_extras import build_card_extras


def tracked_store(branch: str = "a") -> tuple[StackStore, int, str]:
    store = StackStore(default_db_path())
    repo_id = store.ensure_repo(
        RepoIdentity(
            git_common_dir=default_db_path().parent / "x" / ".git",
            path=default_db_path().parent / "x",
            remote_url=None,
        )
    )
    store.set_parent(repo_id, branch, "main")
    (facts,) = store.tracked_branch_facts()
    return store, repo_id, facts.task_id


def finished_run(store: StackStore, repo_id: int, task_id: str, cost: float, at):
    run_id = store.start_agent_run(
        task_id=task_id,
        repo_id=repo_id,
        branch="a",
        trigger="fix_ci",
        head_sha="abc",
        model="claude-haiku-5-5",
        at=at,
    )
    store.finish_agent_run(
        run_id,
        state="succeeded",
        ended_at=at + timedelta(minutes=3),
        turns=4,
        cost_usd=cost,
        tokens=TokenCounts(),
        summary="Re-ran the flaky job.",
    )


def test_a_branch_an_agent_may_take_says_what_it_would_do_with_no_reason():
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    store, repo_id, _ = tracked_store()

    (branch,) = build_card_extras(r.dispatcher, store, NOW).branches

    assert branch.id == f"{repo_id}:a"
    assert (branch.agent.has_action, branch.agent.trigger) == (True, "fix_ci")
    assert branch.agent.reason is None and branch.agent.latest_run is None


def test_a_held_back_branch_carries_the_reason_and_whether_a_person_can_override_it():
    r = rig(a=PrSnapshot(status(), None))
    r.holders["a"] = (holder("agent", "idle"),)
    r.tick()
    store, _, _ = tracked_store()

    (branch,) = build_card_extras(r.dispatcher, store, NOW).branches

    assert branch.agent.reason is not None and branch.agent.reason.code == "claimed"
    assert branch.agent.can_override is False


def test_a_branch_with_no_agent_action_says_so():
    r = rig()
    r.tick()
    store, _, _ = tracked_store()

    (branch,) = build_card_extras(r.dispatcher, store, NOW).branches

    assert (branch.agent.has_action, branch.agent.trigger) == (False, None)


def test_the_latest_run_and_the_autonomous_cost_come_from_the_run_records():
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    store, repo_id, task_id = tracked_store()
    finished_run(store, repo_id, task_id, 0.02, NOW - timedelta(hours=5))
    finished_run(store, repo_id, task_id, 0.03, NOW - timedelta(hours=1))

    (branch,) = build_card_extras(r.dispatcher, store, NOW).branches

    assert branch.agent.runs == 2
    assert branch.agent.latest_run is not None
    assert branch.agent.latest_run.cost_usd == 0.03
    assert branch.agent.latest_run.summary == "Re-ran the flaky job."
    assert branch.spend.autonomous_usd == 0.05
    assert branch.spend.autonomous_runs == 2


def test_interactive_cost_is_what_claims_attribute_to_the_task_over_all_time():
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    store, _, task_id = tracked_store()
    long_ago = NOW - timedelta(days=90)
    claim(store, task_id, long_ago, long_ago + timedelta(minutes=10))
    store.add_usage_calls(
        [call("m1", long_ago + timedelta(minutes=5), cost=2.5)],
        transcript="t",
        offset=1,
    )

    (branch,) = build_card_extras(r.dispatcher, store, NOW).branches

    assert branch.spend.interactive_usd == 2.5
    assert branch.spend.autonomous_usd == 0.0


def test_the_extras_say_when_the_agents_last_decided_and_spend_was_last_read():
    r = rig(a=PrSnapshot(status(), None))
    store, _, _ = tracked_store()

    before = build_card_extras(r.dispatcher, store, NOW)
    r.tick()
    after = build_card_extras(r.dispatcher, store, NOW, NOW - timedelta(seconds=40))

    assert (before.agents_decided_at, before.spend_read_at) == (None, None)
    assert after.agents_decided_at == NOW.isoformat()
    assert after.spend_read_at == (NOW - timedelta(seconds=40)).isoformat()


def test_the_endpoint_serves_the_same_extras():
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    store, repo_id, _ = tracked_store()
    store.close()
    app = create_app(
        tracker=lambda: None, wall_clock=lambda: NOW, dispatcher=r.dispatcher
    )

    body = api.CardExtras.model_validate(TestClient(app).get("/api/card-extras").json())

    assert [b.id for b in body.branches] == [f"{repo_id}:a"]
