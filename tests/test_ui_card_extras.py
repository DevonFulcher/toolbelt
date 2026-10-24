"""The task card's agent and spend parts (`build_card_extras`)."""

from datetime import UTC, datetime, timedelta

from test_dispatcher import NOW, PrSnapshot, holder, rig, status
from test_token_spend import call, claim

from toolbelt.git.stack.store import (
    RepoIdentity,
    StackStore,
    TokenCounts,
    default_db_path,
)
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
    assert branch.agent.recent_runs == []


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
    # Every run of the branch, newest first, each with its own cost.
    assert [run.cost_usd for run in branch.agent.recent_runs] == [0.03, 0.02]
    assert branch.agent.recent_runs[0] == branch.agent.latest_run
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


def test_a_cards_next_lane_is_the_fold_of_its_prs_history():
    from datetime import UTC

    from test_ui_aggregate import MONDAY, pr, ready

    from toolbelt.git.stack.store import PrCandidate, PrLink

    r = rig(a=PrSnapshot(status(), None))
    store, repo_id, _ = tracked_store()
    created = MONDAY.astimezone(UTC)
    store.link_pr(repo_id, "a", [PrCandidate(PrLink(1, "node"), created)])
    record = pr(1, created=0, draft=True, items=(ready(13),))
    branch_now = MONDAY.replace(hour=20)

    (extras,) = build_card_extras(
        r.dispatcher,
        store,
        branch_now,
        pr_facts=lambda rid, name: (record.facts, "none"),
    ).branches

    (nxt,) = [lane for lane in extras.lanes if lane.lane == "next"]
    assert [(p.kind, p.start, p.end) for p in nxt.phases] == [
        ("next_publish", "2026-10-05T00:00:00Z", "2026-10-05T13:00:00Z"),
        ("next_request_review", "2026-10-05T13:00:00Z", None),
    ]
    assert extras.lanes[0].lane == "next"


def test_a_cards_next_lane_uses_the_looked_up_pr_even_when_the_db_has_no_link():
    from test_ui_aggregate import pr, ready

    r = rig(a=PrSnapshot(status(), None))
    store, _, _ = tracked_store()
    record = pr(1, created=0, draft=True, items=(ready(13),))

    (extras,) = build_card_extras(
        r.dispatcher,
        store,
        record.facts.created_at.replace(hour=20),
        pr_facts=lambda rid, name: (record.facts, "none"),
    ).branches

    (nxt,) = [lane for lane in extras.lanes if lane.lane == "next"]
    assert [p.kind for p in nxt.phases] == ["next_publish", "next_request_review"]


def test_a_branch_with_no_pr_looked_up_is_open_pr_from_its_start():
    r = rig(a=PrSnapshot(status(), None))
    store, _, _ = tracked_store()
    now = datetime.now(UTC) + timedelta(hours=1)

    (extras,) = build_card_extras(r.dispatcher, store, now).branches

    (nxt,) = [lane for lane in extras.lanes if lane.lane == "next"]
    assert [p.kind for p in nxt.phases] == ["next_open_pr"]


def test_a_task_with_no_pr_can_be_validated_and_shows_where_it_stands():
    r = rig(a=PrSnapshot(status(), None))
    store, repo_id, task_id = tracked_store()

    def validated(hours_ago: float, result: str) -> None:
        run_id = store.start_agent_run(
            task_id=task_id,
            repo_id=repo_id,
            branch="a",
            trigger="validate",
            head_sha="abc",
            model="claude-haiku-5-5",
            at=NOW - timedelta(hours=hours_ago + 1),
        )
        store.finish_agent_run(
            run_id,
            state="succeeded",
            ended_at=NOW - timedelta(hours=hours_ago),
            result=result,
        )

    (before,) = build_card_extras(r.dispatcher, store, NOW).branches
    assert before.agent.validation.model_dump() == {
        "can_validate": True,
        "validated": False,
        "result": None,
        "verdict": None,
        "opening_pr": False,
        "notice": None,
    }

    validated(3, "fail")
    (waiting,) = build_card_extras(
        r.dispatcher, store, NOW, opening_pr=lambda *_: (True, "boom")
    ).branches
    assert waiting.agent.validation.validated is True
    assert waiting.agent.validation.result == "fail"
    assert (waiting.agent.validation.opening_pr, waiting.agent.validation.notice) == (
        True,
        "boom",
    )
    assert {lane.lane for lane in waiting.lanes} >= {"agent", "personal_review"}

    store.add_review_verdict(
        task_id=task_id,
        repo_id=repo_id,
        branch="a",
        head_sha="abc",
        verdict="changes_requested",
        note="rename it",
        at=NOW - timedelta(hours=2),
    )
    (sent_back,) = build_card_extras(r.dispatcher, store, NOW).branches
    assert sent_back.agent.validation.validated is False
    assert sent_back.agent.validation.verdict.note == "rename it"  # type: ignore[union-attr]


def test_a_task_with_a_pr_cannot_be_validated():
    from test_ui_aggregate import MONDAY, pr

    r = rig(a=PrSnapshot(status(), None))
    store, _, _ = tracked_store()
    record = pr(1, created=0, draft=True)
    (branch,) = build_card_extras(
        r.dispatcher,
        store,
        MONDAY.replace(hour=20),
        pr_facts=lambda rid, name: (record.facts, "none"),
    ).branches

    assert branch.agent.validation.can_validate is False


def test_a_card_with_a_pr_carries_when_it_will_be_finished():
    from test_ui_aggregate import at, pr, request
    from test_ui_estimate import HOURS, day_at, history_records, run_job

    r = rig(a=PrSnapshot(status(), None))
    store, _, _ = tracked_store()
    now = at(day_at(61, 11))
    run_job(default_db_path(), history_records(60), now)
    opened = day_at(61, 10)
    record = pr(
        1,
        created=opened,
        items=(request(opened + 0.25, "bob"),),
        review_now="requested",
    )

    (extras,) = build_card_extras(
        r.dispatcher,
        store,
        now,
        pr_facts=lambda rid, name: (record.facts, "requested"),
        working_hours=HOURS,
    ).branches

    estimate = extras.estimate
    assert estimate is not None and estimate.known
    assert estimate.stage == "wait_review"
    assert "waiting on bob" in estimate.explanation
    assert estimate.finish_p50 is not None and estimate.finish_p80 is not None
    assert estimate.finish_p50 <= estimate.finish_p80
    assert estimate.finish_p50 > now.isoformat()


def test_a_card_without_a_pr_has_no_estimate():
    r = rig(a=PrSnapshot(status(), None))
    store, _, _ = tracked_store()

    (extras,) = build_card_extras(r.dispatcher, store, NOW).branches

    assert extras.estimate is None
