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
from toolbelt.diff_review.revisions import FileDiff, RevisionPair
from toolbelt.git.stack.status import NO_PR, BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import (
    AgentRun,
    FileApproval,
    RepoIdentity,
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
    UNREVIEWED,
    ReviewRefused,
    file_reviews,
    fully_approved,
    open_pr,
    validated_since,
    validated_tasks,
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


def test_a_task_is_validated_from_its_first_succeeded_validate_run():
    runs = [validate_run(5), validate_run(2)]

    assert validated_since(runs) == at(2)


def test_only_a_run_that_succeeded_validates_and_only_a_validate_run():
    runs = [
        validate_run(1, state="failed"),
        validate_run(2, state="deciding"),
        validate_run(3, state="timed_out"),
        validate_run(4, trigger="fix_ci"),
    ]

    assert validated_since(runs) is None


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
    run_id = store.start_agent_run(
        task_id=ids["a"],
        repo_id=repo_id,
        branch="a",
        trigger=VALIDATE,
        head_sha="abc",
        model="m",
        at=T0,
    )
    store.finish_agent_run(run_id, state="succeeded", ended_at=at(1), result="pass")
    try:
        assert validated_tasks(store) == {ids["a"]}
        (run,) = store.agent_runs()
        assert run.result == "pass"
    finally:
        store.close()


def diff(path: str, hash_: str) -> FileDiff:
    return FileDiff(path=path, old_path=None, status="M", diff_hash=hash_)


def approval(path: str, hash_: str, state: str, hours: float = 1) -> FileApproval:
    return FileApproval(
        id=int(hours * 10),
        task_id="t1",
        repo_id=1,
        branch="a",
        path=path,
        diff_hash=hash_,
        state=state,
        note=f"note on {path}",
        decided_at=at(hours),
    )


def test_a_decision_counts_only_for_its_path_and_the_hash_of_its_diff():
    diffs = [diff("a.py", "h1"), diff("b.py", "h2")]
    decisions = [
        approval("a.py", "h1", APPROVED),
        approval("b.py", "old", APPROVED),  # b's diff changed since
        approval("gone.py", "h9", APPROVED),  # no longer in the diff
    ]

    reviews = file_reviews(diffs, decisions)

    assert [(r.diff.path, r.state) for r in reviews] == [
        ("a.py", APPROVED),
        ("b.py", UNREVIEWED),
    ]
    assert not fully_approved(reviews)


def test_the_latest_decision_for_a_diff_wins_and_all_files_must_be_approved():
    diffs = [diff("a.py", "h1")]
    asked = [
        approval("a.py", "h1", APPROVED, 1),
        approval("a.py", "h1", CHANGES_REQUESTED, 2),
    ]

    (review,) = file_reviews(diffs, asked)
    assert (review.state, review.note) == (CHANGES_REQUESTED, "note on a.py")

    again = [*asked, approval("a.py", "h1", APPROVED, 3)]
    assert fully_approved(file_reviews(diffs, again))
    assert not fully_approved([])  # nothing to approve is not approved


def hashes(repo: Path, base: str = "main", head: str = "devon/a") -> dict[str, str]:
    pair = RevisionPair.resolve(repo, base=base, head=head)
    return {d.path: d.diff_hash for d in pair.file_diffs()}


def commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    git("add", name, cwd=repo)
    git("commit", "-m", message, cwd=repo)


def test_a_commit_that_does_not_touch_a_file_keeps_its_hash(repo: Path):
    git("switch", "-c", "devon/a", cwd=repo)
    commit(repo, "a.txt", "one\n", "Add a")
    before = hashes(repo)

    commit(repo, "b.txt", "two\n", "Add b")
    after = hashes(repo)

    assert after["a.txt"] == before["a.txt"]
    assert set(after) == {"a.txt", "b.txt"}


def test_a_rebase_onto_a_moved_parent_keeps_the_hash_of_an_unchanged_diff(repo: Path):
    commit(repo, "f.txt", "".join(f"line {n}\n" for n in range(1, 11)), "Add f")
    git("switch", "-c", "devon/a", cwd=repo)
    commit(
        repo,
        "f.txt",
        "".join(f"line {n}\n" for n in range(1, 10)) + "line 10 edited\n",
        "Edit the end of f",
    )
    before = hashes(repo)
    git("switch", "main", cwd=repo)
    # The parent moves: three lines are added at the top of f, so every line
    # number in the branch's patch shifts.
    commit(
        repo,
        "f.txt",
        "new 1\nnew 2\nnew 3\n" + "".join(f"line {n}\n" for n in range(1, 11)),
        "Parent adds lines on top",
    )
    git("switch", "devon/a", cwd=repo)
    git("rebase", "main", cwd=repo)

    assert hashes(repo) == before


def test_an_edit_to_the_file_changes_its_hash_and_not_the_others(repo: Path):
    git("switch", "-c", "devon/a", cwd=repo)
    commit(repo, "a.txt", "one\n", "Add a")
    commit(repo, "b.txt", "two\n", "Add b")
    before = hashes(repo)

    commit(repo, "a.txt", "one edited\n", "Edit a")
    after = hashes(repo)

    assert after["a.txt"] != before["a.txt"]
    assert after["b.txt"] == before["b.txt"]


def test_a_file_that_enters_the_diff_is_new_and_one_that_leaves_it_is_gone(repo: Path):
    git("switch", "-c", "devon/a", cwd=repo)
    commit(repo, "a.txt", "one\n", "Add a")
    commit(repo, "b.txt", "two\n", "Add b")
    approved = {path: h for path, h in hashes(repo).items()}
    decisions = [approval(path, h, APPROVED) for path, h in approved.items()]

    git("rm", "-q", "b.txt", cwd=repo)
    git("commit", "-m", "Drop b", cwd=repo)
    commit(repo, "c.txt", "three\n", "Add c")
    pair = RevisionPair.resolve(repo, base="main", head="devon/a")
    reviews = {r.diff.path: r.state for r in file_reviews(pair.file_diffs(), decisions)}

    assert reviews == {"a.txt": APPROVED, "c.txt": UNREVIEWED}  # b left, c entered


def test_a_renamed_file_is_one_file_and_its_hash_survives_other_commits(repo: Path):
    commit(repo, "old.txt", "".join(f"line {n}\n" for n in range(1, 21)), "Add old")
    git("switch", "-c", "devon/a", cwd=repo)
    git("mv", "old.txt", "new.txt", cwd=repo)
    git("commit", "-m", "Rename", cwd=repo)
    pair = RevisionPair.resolve(repo, base="main", head="devon/a")
    (renamed,) = pair.file_diffs()
    assert (renamed.status, renamed.old_path, renamed.path) == (
        "R",
        "old.txt",
        "new.txt",
    )

    commit(repo, "other.txt", "x\n", "Elsewhere")
    after = {
        d.path: d
        for d in RevisionPair.resolve(repo, base="main", head="devon/a").file_diffs()
    }

    assert after["new.txt"].diff_hash == renamed.diff_hash
    assert after["new.txt"].old_path == "old.txt"


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
        commit(repo, "a.txt", "one\n", "Add a")
        commit(repo, "b.txt", "two\n", "Add b")
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


def files_of(r) -> dict[str, api.FileReviewInfo]:
    state = state_of(r)
    assert state is not None
    return {f.path: f for f in state.files}


def decide(
    r, path: str, state: str = "approved", note: str = "", hash_: str | None = None
):
    return r.client.post(
        "/api/review/file",
        json={
            "id": f"{r.repo_id}:devon/a",
            "path": path,
            "diff_hash": hash_ or files_of(r)[path].diff_hash,
            "state": state,
            "note": note,
        },
    )


def test_the_card_reads_personal_review_and_the_review_view_gets_its_files(review):
    state = state_of(review)

    assert state is not None
    assert (state.result, state.summary) == ("pass", "Fine, one nit.")
    assert {(f.path, f.state) for f in state.files} == {
        ("a.txt", "unreviewed"),
        ("b.txt", "unreviewed"),
    }
    assert not state.opening_pr
    assert next_of(review.client)["who"] == "you"


def test_the_pr_opens_when_the_last_file_is_approved_and_not_before(review):
    r = review

    first = decide(r, "a.txt", note="fine")

    assert first.status_code == 202
    assert files_of(r)["a.txt"].state == "approved"
    assert files_of(r)["a.txt"].note == "fine"
    assert files_of(r)["b.txt"].state == "unreviewed"
    assert r.opened == []
    assert next_of(r.client)["kind"] == "personal_review"

    decide(r, "b.txt")

    r.wait_for(lambda: r.opened == [("devon/a", "main")], "the PR to be opened")
    r.wait_for(lambda: next_of(r.client)["kind"] != "personal_review", "the card")
    assert state_of(r) is None  # once the PR exists there is nothing left to ask


def test_a_commit_elsewhere_keeps_an_approval_and_an_edit_to_the_file_drops_it(review):
    r = review
    decide(r, "a.txt")

    commit(r.repo, "b.txt", "two edited\n", "Edit b")
    assert files_of(r)["a.txt"].state == "approved"  # a's own diff did not change
    assert files_of(r)["b.txt"].state == "unreviewed"

    commit(r.repo, "a.txt", "one edited\n", "Edit a")
    assert files_of(r)["a.txt"].state == "unreviewed"  # a's diff changed
    assert r.opened == []


def test_a_decision_for_a_diff_that_has_changed_is_refused(review):
    r = review
    stale = files_of(r)["a.txt"].diff_hash
    commit(r.repo, "a.txt", "one edited\n", "Edit a")

    response = decide(r, "a.txt", hash_=stale)

    assert response.status_code == 409
    assert "changed since this page loaded" in response.json()["detail"]
    assert decide(r, "nope.txt", hash_="x").status_code == 409


def test_requesting_changes_to_a_file_blocks_the_pr_and_keeps_the_task_in_review(
    review,
):
    r = review
    decide(r, "a.txt", "changes_requested", "rename it")
    decide(r, "b.txt")

    assert files_of(r)["a.txt"].state == "changes_requested"
    assert files_of(r)["a.txt"].note == "rename it"
    assert r.opened == []
    assert next_of(r.client)["kind"] == "personal_review"

    decide(r, "a.txt")  # once it is changed (or reconsidered) and approved

    r.wait_for(lambda: r.opened == [("devon/a", "main")], "the PR to be opened")


def test_approve_all_approves_the_diff_as_shown_and_opens_the_pr(review):
    r = review
    shown = {path: f.diff_hash for path, f in files_of(r).items()}

    response = r.client.post(
        "/api/review/approve-all",
        json={"id": f"{r.repo_id}:devon/a", "files": shown, "note": "all good"},
    )

    assert response.status_code == 202
    r.wait_for(lambda: r.opened == [("devon/a", "main")], "the PR to be opened")
    store = StackStore(default_db_path())
    try:
        stored = store.file_approvals()
    finally:
        store.close()
    assert {(a.path, a.state, a.note) for a in stored} == {
        ("a.txt", "approved", "all good"),
        ("b.txt", "approved", "all good"),
    }


def test_approve_all_refuses_a_diff_that_is_not_the_one_shown(review):
    r = review
    shown = {path: f.diff_hash for path, f in files_of(r).items()}
    commit(r.repo, "a.txt", "one edited\n", "Edit a")

    response = r.client.post(
        "/api/review/approve-all",
        json={"id": f"{r.repo_id}:devon/a", "files": shown, "note": ""},
    )

    assert response.status_code == 409
    assert r.opened == []


def test_a_failed_pr_is_reported_and_approve_all_tries_again(review):
    r = review
    r.open_fails = True
    shown = {path: f.diff_hash for path, f in files_of(r).items()}
    body = {"id": f"{r.repo_id}:devon/a", "files": shown, "note": ""}
    r.client.post("/api/review/approve-all", json=body)
    r.wait_for(
        lambda: state_of(r) is not None and state_of(r).notice is not None,  # type: ignore[union-attr]
        "the failure to be reported",
    )
    assert state_of(r).notice == "Opening the PR failed: boom"  # type: ignore[union-attr]
    assert all(f.state == "approved" for f in files_of(r).values())

    r.open_fails = False
    r.client.post("/api/review/approve-all", json=body)

    r.wait_for(lambda: r.opened == [("devon/a", "main")], "the PR to be opened")


def test_a_task_that_was_not_validated_cannot_be_reviewed(review):
    r = review
    store = StackStore(default_db_path())
    store.set_parent(r.repo_id, "devon/b", "main")
    store.close()

    response = r.client.post(
        "/api/review/file",
        json={
            "id": f"{r.repo_id}:devon/b",
            "path": "a.txt",
            "diff_hash": "x",
            "state": "approved",
            "note": "",
        },
    )

    assert response.status_code == 409


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
