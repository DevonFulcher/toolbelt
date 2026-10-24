"""Validate, personal review, publish: the Next kind, the replay of its inputs,
the validate job (eligibility, dispatcher, run), and approving a branch (the PR
is opened through a fake `gh`, never GitHub)."""

import asyncio
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import git
from fastapi.testclient import TestClient
from test_agent_eligibility import NOW as ELIGIBILITY_NOW
from test_agent_eligibility import run as eligibility_run
from test_agent_eligibility import task as facts_of
from test_agent_eligibility import world
from test_agent_runner import Rig as RunnerRig
from test_agent_runner import assistant, decision, result
from test_dispatcher import NOW, OverrideRefused, PrSnapshot, rig, status

from toolbelt.autonomous_agents.eligibility import validate_blocker
from toolbelt.autonomous_agents.instructions import (
    build_instructions,
    build_prompt,
    report_verdict,
)
from toolbelt.autonomous_agents.limits import VALIDATE, AgentLimits
from toolbelt.git.stack.status import NO_PR, BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import (
    AgentRun,
    RepoIdentity,
    ReviewVerdict,
    StackStore,
    TokenCounts,
    default_db_path,
)
from toolbelt.ui import api_models as api
from toolbelt.ui import next_action as na
from toolbelt.ui.app import create_app
from toolbelt.ui.personal_review import (
    APPROVED,
    CHANGES_REQUESTED,
    ReviewRefused,
    current_verdict,
    open_pr,
    validated_tasks,
    validation_spans,
)
from toolbelt.ui.pr_activity import PrFacts

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def validate_run(
    ended_hours: float | None, state: str = "succeeded", trigger: str = VALIDATE
) -> AgentRun:
    return AgentRun(
        run_id=f"r{ended_hours}",
        task_id="t1",
        repo_id=1,
        branch="a",
        trigger=trigger,
        head_sha="abc",
        model="m",
        state=state,
        started_at=at((ended_hours or 1) - 0.1),
        ended_at=at(ended_hours) if ended_hours is not None else None,
        session_id=None,
        turns=1,
        cost_usd=0.1,
        tokens=TokenCounts(),
        summary=None,
        error=None,
    )


def verdict(hours: float, kind: str, head: str = "abc") -> ReviewVerdict:
    return ReviewVerdict(
        id=int(hours * 10),
        task_id="t1",
        repo_id=1,
        branch="a",
        head_sha=head,
        verdict=kind,
        note="n",
        at=at(hours),
    )


# ---- the Next kind ------------------------------------------------------------------------


def test_a_validated_branch_with_no_pr_reads_personal_review_and_waits_on_you():
    action = na.next_action(NO_PR, None, validated=True)

    assert (action.kind, action.label, action.who) == (
        na.PERSONAL_REVIEW,
        "Personal review",
        na.WHO_YOU,
    )


def test_a_branch_with_no_pr_that_is_not_validated_still_reads_open_pr():
    assert na.next_action(NO_PR, None).kind == na.OPEN_PR


def test_validation_means_nothing_once_the_branch_has_a_pr():
    open_pr_status = BranchStatus(PrState.OPEN, CiState.SUCCESS, ReviewState.NONE, None)

    assert (
        na.next_action(open_pr_status, None, validated=True).kind == na.REQUEST_REVIEW
    )


def test_a_pending_question_comes_before_personal_review():
    action = na.next_action(NO_PR, None, deciding=True, validated=True)

    assert (action.kind, action.would_be.kind) == (na.DECIDING, na.PERSONAL_REVIEW)  # type: ignore[union-attr]


# ---- the replay of its inputs --------------------------------------------------------------


def test_a_succeeded_validate_run_validates_until_changes_are_requested():
    runs = [validate_run(2)]

    assert validation_spans(runs, []) == [(at(2), None)]
    assert validation_spans(runs, [verdict(3, CHANGES_REQUESTED)]) == [(at(2), at(3))]


def test_a_later_validate_run_validates_again_after_changes_were_requested():
    runs = [validate_run(2), validate_run(5)]
    verdicts = [verdict(3, CHANGES_REQUESTED)]

    assert validation_spans(runs, verdicts) == [(at(2), at(3)), (at(5), None)]


def test_only_a_run_that_succeeded_validates_and_only_a_validate_run():
    runs = [
        validate_run(1, state="failed"),
        validate_run(2, state="deciding"),
        validate_run(3, state="timed_out"),
        validate_run(4, trigger="fix_ci"),
    ]

    assert validation_spans(runs, []) == []


def test_an_approval_does_not_end_validation_but_only_stands_for_its_commit():
    runs = [validate_run(2)]
    approved = [verdict(3, APPROVED, head="abc")]

    assert validation_spans(runs, approved) == [(at(2), None)]
    assert current_verdict(approved, "abc") == approved[0]
    assert current_verdict(approved, "def") is None  # a new commit invalidates it


def test_a_request_for_changes_stands_whatever_the_head():
    requested = [verdict(3, CHANGES_REQUESTED, head="abc")]

    assert current_verdict(requested, "def") == requested[0]


def test_validated_tasks_reads_the_tables():
    store = StackStore(default_db_path())
    repo_id = store.ensure_repo(
        RepoIdentity(
            git_common_dir=default_db_path().parent / "x" / ".git",
            path=default_db_path().parent / "x",
            remote_url=None,
        )
    )
    store.set_parent(repo_id, "a", "main")
    store.set_parent(repo_id, "b", "main")
    ids = {f.name: f.task_id for f in store.tracked_branch_facts()}
    for name in ("a", "b"):
        run_id = store.start_agent_run(
            task_id=ids[name],
            repo_id=repo_id,
            branch=name,
            trigger=VALIDATE,
            head_sha="abc",
            model="m",
            at=T0,
        )
        store.finish_agent_run(run_id, state="succeeded", ended_at=at(1), result="pass")
    store.add_review_verdict(
        task_id=ids["b"],
        repo_id=repo_id,
        branch="b",
        head_sha="abc",
        verdict=CHANGES_REQUESTED,
        note="x",
        at=at(2),
    )
    try:
        assert validated_tasks(store) == {ids["a"]}
        (run, _) = store.agent_runs()
        assert run.result == "pass"
    finally:
        store.close()


# ---- the validate job ----------------------------------------------------------------------


def test_a_validate_run_is_for_a_branch_with_no_pr():
    code = validate_blocker(facts_of(action_kind=VALIDATE, has_pr=True), world())

    assert code is not None and code.code == "has_pr"


def test_a_validate_run_does_not_need_the_agents_switch_the_screen_or_an_open_pr():
    blocker = validate_blocker(
        facts_of(action_kind=VALIDATE, pr_open=False, last_commit_at=None),
        world(enabled=False, screen=None),
    )

    assert blocker is None


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"deciding": True}, "deciding"),
        ({"claimed_by_other": True}, "claimed"),
        ({"has_worktree": False}, "no_worktree"),
        ({"worktree_busy": True}, "worktree_busy"),
        ({"worktree_clean": False}, "worktree_dirty"),
    ],
)
def test_what_is_unsafe_still_stops_a_validate_run(changes, code):
    blocker = validate_blocker(facts_of(action_kind=VALIDATE, **changes), world())

    assert blocker is not None and blocker.code == code


def test_the_task_and_global_limits_apply_to_a_validate_run():
    limits = AgentLimits()
    spent = [
        eligibility_run(
            started=ELIGIBILITY_NOW - timedelta(hours=1 + i),
            cost=limits.task_usd_per_day,
            trigger=VALIDATE,
        )
        for i in range(1)
    ]

    blocker = validate_blocker(facts_of(action_kind=VALIDATE), world(runs=spent))
    day = validate_blocker(
        facts_of(action_kind=VALIDATE),
        world(runs=[eligibility_run(task_id="other", cost=limits.global_usd_per_day)]),
    )
    working = validate_blocker(
        facts_of(action_kind=VALIDATE),
        world(runs=[eligibility_run(state="running")]),
    )

    assert blocker is not None and blocker.code == "task_usd_per_day"
    assert day is not None and day.code == "global_usd_per_day"
    assert working is not None and working.code == "working"


def test_validate_by_hand_launches_a_validate_decision_against_the_parent():
    r = rig(a=PrSnapshot(NO_PR, None))
    r.dispatcher._parent_of = lambda repo_id, branch: "devon/parent"
    r.enabled = False  # the agents' own switch is not needed

    asyncio.run(r.dispatcher.validate_now((1, "a")))

    assert r.launched == ["a"]


def test_validate_by_hand_refuses_a_branch_that_has_a_pr():
    r = rig(a=PrSnapshot(status(), None))

    with pytest.raises(OverrideRefused) as refused:
        asyncio.run(r.dispatcher.validate_now((1, "a")))

    assert refused.value.reason.code == "has_pr"
    assert r.launched == []


def test_validate_by_hand_reports_a_refusal_on_the_task_and_clears_when_started():
    async def scenario():
        r = rig(a=PrSnapshot(NO_PR, None))
        r.state = r.state.__class__(
            exists=True, head_sha="abc", last_commit_at=None, clean=False, busy=False
        )
        r.dispatcher.request_validate((1, "a"))
        assert (1, "a") in r.dispatcher.starting
        await r.dispatcher.idle()
        return r

    r = asyncio.run(scenario())

    assert r.dispatcher.start_errors[(1, "a")] == (
        "The worktree has uncommitted changes."
    )
    assert (1, "a") not in r.dispatcher.starting
    with pytest.raises(KeyError):
        r.dispatcher.request_validate((1, "nope"))


def test_the_validate_instructions_review_read_only_against_the_parent():
    d = decision(VALIDATE, base_ref="devon/parent", pr_number=None, pr_url=None)

    text = build_instructions(d, AgentLimits())

    assert "git diff devon/parent...HEAD" in text
    assert "READ-ONLY" in text
    assert "There is no PR and there is no CI" in text
    assert "Code quality and clarity" in text
    assert '"verdict": "approve" or "changes"' in text
    assert "devon/a" in build_prompt(d) and "devon/parent" in build_prompt(d)


def test_the_verdict_comes_from_the_last_line_of_the_report():
    assert report_verdict('Review.\n{"summary": "s", "verdict": "approve"}') == "pass"
    assert report_verdict('{"summary": "s", "verdict": "changes"}') == "fail"
    assert report_verdict('{"summary": "s"}') is None
    assert report_verdict("not json") is None


def test_a_validate_run_records_its_verdict_and_cannot_edit(tmp_path: Path):
    final = result(result='Looks good.\n{"summary": "Fine.", "verdict": "changes"}')
    rig_ = RunnerRig(tmp_path, [assistant(), final])

    run = rig_.run(decision(VALIDATE))

    assert (run.state, run.trigger, run.result) == ("succeeded", VALIDATE, "fail")
    assert run.summary == "Fine."
    tools = rig_.calls[0]["options"].allowed_tools
    assert "Edit" not in tools and "Write" not in tools and "Bash" in tools


def test_a_run_for_another_job_has_no_verdict(tmp_path: Path):
    run = RunnerRig(tmp_path, [assistant(), result()]).run(decision("fix_ci"))

    assert run.result is None


# ---- approving: the PR is opened through a fake gh --------------------------------------------


class FakeProcess:
    """Stands in for ``logged_process.run``: records the commands, answers
    them like git and gh would."""

    def __init__(self, *, push_fails: bool = False, create_fails: bool = False):
        self.calls: list[list[str]] = []
        self.push_fails = push_fails
        self.create_fails = create_fails

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] == "git" and self.push_fails:
            return subprocess.CompletedProcess(args, 1, "", "no route to origin")
        if args[0] == "gh" and self.create_fails:
            return subprocess.CompletedProcess(args, 1, "", "validation failed")
        out = "https://github.com/o/r/pull/9\n" if args[0] == "gh" else ""
        return subprocess.CompletedProcess(args, 0, out, "")


def test_open_pr_pushes_the_branch_and_opens_a_ready_pr_against_the_parent(tmp_path):
    fake = FakeProcess()

    url = open_pr(tmp_path, "devon/a", "devon/parent", fake)

    assert url == "https://github.com/o/r/pull/9"
    push, create = fake.calls
    assert push == ["git", "push", "-u", "origin", "devon/a"]
    assert create == [
        "gh",
        "pr",
        "create",
        "--base",
        "devon/parent",
        "--head",
        "devon/a",
        "--fill",
    ]
    assert "--draft" not in create


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeProcess(push_fails=True), "Pushing devon/a failed: no route to origin"),
        (FakeProcess(create_fails=True), "Opening the PR failed: validation failed"),
    ],
)
def test_open_pr_says_what_failed(tmp_path, fake, message):
    with pytest.raises(ReviewRefused, match=message):
        open_pr(tmp_path, "devon/a", "main", fake)


class Rig:
    """The app over a real clone with a branch, a bare origin and a fake PR
    opener; the branch has been validated."""

    def __init__(self, repo: Path, git_remote: Path):
        self.repo = repo
        git("switch", "-c", "devon/a", cwd=repo)
        (repo / "a.txt").write_text("one\n")
        git("add", "a.txt", cwd=repo)
        git("commit", "-m", "Add a", cwd=repo)
        git("push", "-q", "origin", "main", cwd=repo)
        store = StackStore(default_db_path())
        self.repo_id = store.ensure_repo(
            RepoIdentity(
                git_common_dir=repo / ".git",
                path=repo,
                remote_url="git@github.com:o/r.git",
            )
        )
        store.set_parent(self.repo_id, "devon/a", "main")
        self.task_id = store.tracked_branch_facts()[0].task_id
        run_id = store.start_agent_run(
            task_id=self.task_id,
            repo_id=self.repo_id,
            branch="devon/a",
            trigger=VALIDATE,
            head_sha="abc",
            model="m",
            at=NOW - timedelta(hours=2),
        )
        store.finish_agent_run(
            run_id,
            state="succeeded",
            ended_at=NOW - timedelta(hours=1),
            summary="Fine, one nit.",
            result="pass",
        )
        store.close()
        self.opened: list[tuple[str, str]] = []
        self.has_pr = False
        self.open_fails = False

        def open_pr_(path: Path, branch: str, base: str) -> str:
            if self.open_fails:
                raise ReviewRefused("Opening the PR failed: boom")
            self.opened.append((branch, base))
            self.has_pr = True
            return "https://github.com/o/r/pull/9"

        async def fetch(branch: str, root: Path) -> PrSnapshot:
            if not self.has_pr:
                return PrSnapshot(NO_PR, None)
            return PrSnapshot(
                BranchStatus(
                    PrState.OPEN,
                    CiState.SUCCESS,
                    ReviewState.NONE,
                    "https://github.com/o/r/pull/9",
                ),
                PrFacts(
                    number=9,
                    title="Add a",
                    author="devon",
                    base_ref="main",
                    head_oid="abc",
                    state="OPEN",
                    is_draft=False,
                    created_at=NOW,
                    merged_at=None,
                    closed_at=None,
                    requested_reviewers=(),
                    commit_rollups=(),
                    items=(),
                ),
            )

        self.app = create_app(fetch=fetch, tick=0.05, open_pr=open_pr_)
        asyncio.run(self.app.state.refresher.run_once())

    @property
    def head(self) -> str:
        return git("rev-parse", "devon/a", cwd=self.repo)

    def wait_for(self, condition, what: str) -> None:
        for _ in range(300):
            if condition():
                return
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def review(repo: Path, git_remote: Path):
    r = Rig(repo, git_remote)
    with TestClient(r.app) as client:
        r.app.state.hub.subscribe()  # the database watcher only runs for a page
        r.client = client  # type: ignore[attr-defined]
        r.wait_for(lambda: next_of(client)["kind"] == "personal_review", "the state")
        yield r


def next_of(client, branch: str = "devon/a") -> dict:
    found = {b["name"]: b for b in client.get("/api/snapshot").json()["branches"]}
    return found[branch]["status"]["next"]


def state_of(r) -> api.PersonalReviewState | None:
    body = r.client.get(
        "/api/review/personal", params={"repo": r.repo_id, "head": "devon/a"}
    ).json()
    return api.PersonalReviewState.model_validate(body) if body else None


def test_the_card_reads_personal_review_and_the_review_view_gets_its_state(review):
    state = state_of(review)

    assert state is not None
    assert (state.head_sha, state.result, state.summary) == (
        review.head,
        "pass",
        "Fine, one nit.",
    )
    assert state.verdict is None and not state.opening_pr
    extras = review.client.get("/api/snapshot").json()
    assert extras["branches"][0]["status"]["next"]["who"] == "you"


def test_approving_opens_the_pr_in_the_background_against_the_parent(review):
    r = review
    request = {"id": f"{r.repo_id}:devon/a", "head_sha": r.head, "note": "lgtm"}

    response = r.client.post("/api/review/approve", json=request)

    assert response.status_code == 202
    r.wait_for(lambda: r.opened == [("devon/a", "main")], "the PR to be opened")
    r.wait_for(lambda: next_of(r.client)["kind"] != "personal_review", "the card")
    # Once the PR exists the Review view has nothing left to ask.
    assert state_of(r) is None
    store = StackStore(default_db_path())
    try:
        (stored,) = store.review_verdicts()
    finally:
        store.close()
    assert (stored.verdict, stored.note, stored.head_sha) == (APPROVED, "lgtm", r.head)


def test_approving_a_commit_that_is_no_longer_the_head_is_refused(review):
    r = review
    stale = r.head
    (r.repo / "a.txt").write_text("two\n")
    git("commit", "-am", "More", cwd=r.repo)

    response = r.client.post(
        "/api/review/approve",
        json={"id": f"{r.repo_id}:devon/a", "head_sha": stale, "note": ""},
    )

    assert response.status_code == 409
    assert "new commit" in response.json()["detail"]
    assert r.opened == []


def test_a_new_commit_invalidates_an_approval_whose_pr_failed(review):
    r = review
    r.open_fails = True
    request = {"id": f"{r.repo_id}:devon/a", "head_sha": r.head, "note": ""}
    r.client.post("/api/review/approve", json=request)
    r.wait_for(
        lambda: (
            (state_of(r) or api.PersonalReviewState).notice  # type: ignore[truthy-function]
            == "Opening the PR failed: boom"
        ),
        "the failure to be reported",
    )
    state = state_of(r)
    assert state is not None and state.verdict.verdict == "approved"  # type: ignore[union-attr]
    assert state.opening_pr is False

    (r.repo / "a.txt").write_text("two\n")
    git("commit", "-am", "More", cwd=r.repo)

    after = state_of(r)
    assert after is not None and after.verdict is None


def test_requesting_changes_sends_the_task_back_to_open_pr(review):
    r = review

    response = r.client.post(
        "/api/review/request-changes",
        json={"id": f"{r.repo_id}:devon/a", "head_sha": r.head, "note": "rename it"},
    )

    assert response.status_code == 200
    r.wait_for(lambda: next_of(r.client)["kind"] == "open_pr", "the card")
    assert state_of(r) is None
    extras = r.client.get("/api/snapshot").json()["branches"][0]["id"]
    assert extras == f"{r.repo_id}:devon/a"
    assert r.opened == []


def test_a_task_that_was_not_validated_cannot_be_approved(review):
    r = review
    r.client.post(
        "/api/review/request-changes",
        json={"id": f"{r.repo_id}:devon/a", "head_sha": r.head, "note": "no"},
    )
    r.wait_for(lambda: next_of(r.client)["kind"] == "open_pr", "the card")

    response = r.client.post(
        "/api/review/approve",
        json={"id": f"{r.repo_id}:devon/a", "head_sha": r.head, "note": ""},
    )

    assert response.status_code == 409
    assert "not waiting for a personal review" in response.json()["detail"]


def test_validating_by_hand_answers_202_and_an_unknown_task_404(review):
    r = review

    assert (
        r.client.post("/api/agents/validate", json={"id": "99:nope"}).status_code == 404
    )
    assert (
        r.client.post(
            "/api/agents/validate", json={"id": f"{r.repo_id}:devon/a"}
        ).status_code
        == 202
    )


def test_the_aggregate_replay_draws_personal_review_in_the_next_lane():
    from toolbelt.ui import aggregate

    lanes = [
        api.TimelineLane(
            lane="personal_review",
            events=[],
            phases=[
                api.LanePhase(
                    start=at(2).isoformat(),
                    end=at(4).isoformat(),
                    kind="personal_review_waiting",
                    detail="",
                    state="",
                )
            ],
        )
    ]

    segments = aggregate.task_segments(None, at(0), at(6), lanes, None, at(6))

    assert [(s.start, s.end, s.state) for s in segments] == [
        (at(0), at(2), "open_pr"),
        (at(2), at(4), "personal_review"),
        (at(4), at(6), "open_pr"),
    ]
