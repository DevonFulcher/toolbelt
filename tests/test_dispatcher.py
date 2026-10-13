"""The dispatcher: which tasks an autonomous agent should be on, and why not.
PR lookups, git, claims, the clock and the screen are all injected."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import git

from toolbelt.autonomous_agents.branch_state import BranchState, inspect_branch
from toolbelt.autonomous_agents.dispatcher import (
    Dispatcher,
    DispatchTask,
    bot_reviews,
    failed_ci_commits,
)
from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.autonomous_agents.screen import ScreenState
from toolbelt.git.stack.status import (
    NO_PR,
    BranchStatus,
    CiState,
    PrState,
    ReviewState,
)
from toolbelt.git.stack.store import AgentRun, TokenCounts
from toolbelt.task_claims.resolution import TrackedTask
from toolbelt.ui.claims_feed import BranchHolder
from toolbelt.ui.pr_activity import PrFacts, TimelineItem
from toolbelt.ui.pr_fetch import PrSnapshot

NOW = datetime(2026, 10, 13, 17, 0, tzinfo=UTC)  # Tuesday, midday local (CDT)


def item(branch: str, repo_id: int = 1, worktree: bool = True) -> DispatchTask:
    return DispatchTask(
        task=TrackedTask(
            repo_id=repo_id,
            repo_name="api",
            branch=branch,
            task_id=f"id-{branch}",
            task_ref=None,
            worktree=Path(f"/wt/{branch}") if worktree else None,
        ),
        repo_path=Path("/repo"),
    )


def status(**changes) -> BranchStatus:
    base = {
        "pr": PrState.OPEN,
        "ci": CiState.FAILED,
        "review": ReviewState.NONE,
        "url": None,
    }
    return BranchStatus(**{**base, **changes})


def facts(items=()) -> PrFacts:
    return PrFacts(
        number=1,
        title="t",
        author="me",
        base_ref="main",
        head_oid="abc",
        state="OPEN",
        is_draft=False,
        created_at=NOW,
        merged_at=None,
        closed_at=None,
        requested_reviewers=(),
        commit_rollups=(),
        items=tuple(items),
    )


def holder(kind: str = "agent", state: str = "idle") -> BranchHolder:
    return BranchHolder(
        holder="h", kind=kind, state=state, link=None, started_at=NOW, stale=False
    )


class Rig:
    """A dispatcher over fakes; tweak the attributes, then ``tick``."""

    def __init__(self, tasks: list[DispatchTask], snapshots: dict[str, PrSnapshot]):
        self.tasks = tasks
        self.snapshots = snapshots
        self.fetched: list[str] = []
        self.enabled = True
        self.screen: ScreenState | None = ScreenState(True, False)
        self.signed_in = True
        self.holders: dict[str, tuple[BranchHolder, ...]] = {}
        self.runs: list[AgentRun] = []
        self.state = BranchState(
            exists=True,
            head_sha="abc",
            last_commit_at=NOW - timedelta(hours=3),
            clean=True,
            busy=False,
        )
        self.launched: list[str] = []
        self.launching = True
        self.monotonic = 0.0

        async def fetch(branch: str, _path: Path) -> PrSnapshot:
            self.fetched.append(branch)
            return self.snapshots[branch]

        async def launch(decision) -> None:
            self.launched.append(decision.branch)

        self.dispatcher = Dispatcher(
            tasks=lambda: self.tasks,
            fetch=fetch,
            holders=lambda repo_id, branch: self.holders.get(branch, ()),
            inspect=lambda _item: self.state,
            runs=lambda: self.runs,
            enabled=lambda: self.enabled,
            limits=AgentLimits(),
            screen=lambda: self.screen,
            signed_in=lambda: self.signed_in,
            wall_clock=lambda: NOW,
            clock=lambda: self.monotonic,
            poll_interval=300,
            launch=launch,
            launching=lambda: self.launching,
        )

    def tick(self) -> None:
        asyncio.run(self.dispatcher.tick())

    def decision(self, branch: str):
        return self.dispatcher.decisions.get((1, branch))


def rig(**snapshots: PrSnapshot) -> Rig:
    return Rig([item(name) for name in snapshots], dict(snapshots))


# ---- which tasks get a decision -------------------------------------------------------------------


def test_only_tasks_whose_next_action_is_an_agents_get_a_decision():
    r = rig(
        ci_failed=PrSnapshot(status(), None),
        conflicts=PrSnapshot(status(ci=CiState.NONE, has_conflicts=True), None),
        comments=PrSnapshot(status(ci=CiState.SUCCESS, open_comments=2), None),
        approved=PrSnapshot(
            status(ci=CiState.SUCCESS, review=ReviewState.APPROVED), None
        ),
        no_pr=PrSnapshot(NO_PR, None),
        waiting=PrSnapshot(status(ci=CiState.RUNNING), None),
    )

    r.tick()

    assert {b: r.decision(b).trigger for (_, b) in r.dispatcher.decisions} == {
        "ci_failed": "fix_ci",
        "conflicts": "resolve_conflicts",
        "comments": "resolve_comments",
    }


def test_a_task_an_agent_may_start_on_has_no_reason_and_is_launched():
    r = rig(a=PrSnapshot(status(), None))

    r.tick()

    assert r.decision("a").may_start
    assert r.launched == ["a"]
    assert r.dispatcher.ticked_at == NOW


def test_with_no_launcher_it_is_a_dry_run_that_only_decides():
    r = rig(a=PrSnapshot(status(), None))
    r.dispatcher._launch = None

    r.tick()

    assert r.decision("a").may_start


# ---- reasons -------------------------------------------------------------------------------------------


def test_agents_off_means_nothing_is_fetched_decided_or_launched():
    r = rig(a=PrSnapshot(status(), None))
    r.enabled = False

    r.tick()

    assert r.dispatcher.decisions == {}
    assert r.fetched == [] and r.launched == []
    assert [b.code for b in r.dispatcher.blockers] == ["disabled"]
    assert r.dispatcher.ticked_at == NOW


def test_a_person_or_interactive_session_holding_the_task_blocks_it():
    r = rig(a=PrSnapshot(status(), None))
    r.holders["a"] = (holder("agent", "idle"),)

    r.tick()

    assert r.decision("a").reason.code == "claimed"


def test_an_autonomous_holder_is_not_another_claimant():
    r = rig(a=PrSnapshot(status(), None))
    r.holders["a"] = (holder("autonomous", "working"),)

    r.tick()

    assert r.decision("a").reason is None


def test_branch_state_reasons():
    r = rig(a=PrSnapshot(status(), None))

    r.state = BranchState(
        True, "abc", NOW - timedelta(hours=1), clean=False, busy=False
    )
    r.tick()
    assert r.decision("a").reason.code == "worktree_dirty"

    r.state = BranchState(True, "abc", NOW - timedelta(hours=1), clean=True, busy=True)
    r.tick()
    assert r.decision("a").reason.code == "worktree_busy"

    r.state = BranchState(True, "abc", NOW - timedelta(days=20), clean=True, busy=False)
    r.tick()
    assert r.decision("a").reason.code == "inactive"


def test_a_branch_with_no_worktree_has_that_reason():
    r = Rig([item("a", worktree=False)], {"a": PrSnapshot(status(), None)})

    r.tick()

    assert r.decision("a").reason.code == "no_worktree"


def test_sign_in_and_the_screen_show_as_reasons_and_in_the_banner():
    r = rig(a=PrSnapshot(status(), None))
    r.screen = ScreenState(True, True)

    r.tick()

    assert r.decision("a").reason.code == "screen_locked"
    assert [b.code for b in r.dispatcher.blockers] == ["screen_locked"]


def test_past_runs_count_toward_the_limits():
    r = rig(a=PrSnapshot(status(), None))
    r.runs = [
        AgentRun(
            run_id=f"r{i}",
            task_id="id-a",
            repo_id=1,
            branch="a",
            trigger="fix_ci",
            head_sha="abc",
            model="m",
            state="succeeded",
            started_at=NOW - timedelta(days=2, hours=i),
            ended_at=NOW - timedelta(days=2, hours=i) + timedelta(minutes=3),
            session_id=None,
            turns=3,
            cost_usd=0.01,
            tokens=TokenCounts(),
            summary=None,
            error=None,
        )
        for i in range(2)
    ]

    r.tick()

    assert r.decision("a").reason.code == "runs_per_head"


# ---- thrash signals ---------------------------------------------------------------------------------


def test_bot_reviews_and_commits_with_failed_ci_are_counted_from_the_pr_history():
    history = facts(
        [
            TimelineItem(NOW, "review", "copilot-pull-request-reviewer[bot]"),
            TimelineItem(NOW, "review", "Copilot"),
            TimelineItem(NOW, "review", "alice"),
            TimelineItem(NOW, "commit", "me", oid="c1", ci="FAILURE"),
            TimelineItem(NOW, "commit", "me", oid="c1", ci="FAILURE"),
            TimelineItem(NOW, "commit", "me", oid="c2", ci="ERROR"),
            TimelineItem(NOW, "commit", "me", oid="c3", ci="SUCCESS"),
        ]
    )

    assert bot_reviews(history) == 2
    assert failed_ci_commits(history) == 2
    assert (bot_reviews(None), failed_ci_commits(None)) == (0, 0)


def test_a_pr_that_keeps_failing_is_left_to_the_user():
    commits = [
        TimelineItem(NOW, "commit", "me", oid=f"c{i}", ci="FAILURE") for i in range(3)
    ]
    r = rig(a=PrSnapshot(status(), facts(commits)))

    r.tick()

    assert r.decision("a").reason.code == "thrash"


# ---- polling GitHub --------------------------------------------------------------------------------


def test_prs_are_refetched_only_after_the_poll_interval():
    r = rig(a=PrSnapshot(status(), None))

    r.tick()
    r.monotonic = 100
    r.tick()
    assert r.fetched == ["a"]

    r.monotonic = 301
    r.tick()
    assert r.fetched == ["a", "a"]


def test_a_state_change_between_polls_is_seen_after_the_next_poll():
    r = rig(a=PrSnapshot(status(), None))
    r.tick()
    assert r.decision("a") is not None

    r.snapshots["a"] = PrSnapshot(status(ci=CiState.SUCCESS), None)
    r.monotonic = 301
    r.tick()

    assert r.decision("a") is None


# ---- git ---------------------------------------------------------------------------------------------


def test_inspect_branch_reads_head_the_last_own_commit_and_a_clean_worktree(
    repo: Path, tmp_path: Path
):
    git("checkout", "-b", "devon/x", cwd=repo)
    (repo / "x.txt").write_text("x\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "own work", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)

    state = inspect_branch(repo, "devon/x", repo)

    assert state.head_sha == head
    assert state.last_commit_at is not None
    assert datetime.now(UTC) - state.last_commit_at < timedelta(minutes=5)
    assert (state.clean, state.busy) == (True, False)


def test_inspect_branch_ignores_merge_commits_and_other_authors(repo: Path):
    git("checkout", "-b", "devon/x", cwd=repo)
    (repo / "x.txt").write_text("x\n")
    git("add", "-A", cwd=repo)
    git("-c", "user.email=other@example.com", "commit", "-m", "theirs", cwd=repo)

    assert inspect_branch(repo, "devon/x", repo).last_commit_at is None


def test_inspect_branch_sees_uncommitted_changes_and_a_merge_in_progress(
    repo: Path, tmp_path: Path
):
    git("checkout", "-b", "devon/x", cwd=repo)
    (repo / "README.md").write_text("mine\n")
    git("commit", "-am", "mine", cwd=repo)
    git("checkout", "-b", "devon/y", "main", cwd=repo)
    (repo / "README.md").write_text("theirs\n")
    git("commit", "-am", "theirs", cwd=repo)
    git("checkout", "devon/x", cwd=repo)

    (repo / "dirty.txt").write_text("d\n")
    assert inspect_branch(repo, "devon/x", repo).clean is False
    (repo / "dirty.txt").unlink()

    import subprocess

    subprocess.run(
        ["git", "merge", "devon/y"], cwd=repo, capture_output=True, check=False
    )
    state = inspect_branch(repo, "devon/x", repo)

    assert state.busy is True


def test_inspect_branch_without_a_worktree_is_not_clean(repo: Path):
    git("checkout", "-b", "devon/x", cwd=repo)
    git("checkout", "main", cwd=repo)

    state = inspect_branch(repo, "devon/x", None)

    assert (state.clean, state.busy) == (False, False)


def test_a_commit_that_came_in_from_the_default_branch_is_not_work_on_the_branch(
    repo: Path,
):
    git("checkout", "-b", "devon/stale", cwd=repo)
    git("checkout", "main", cwd=repo)
    (repo / "main.txt").write_text("m\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "my commit on main", cwd=repo)
    git("push", "origin", "main", cwd=repo)
    git("checkout", "devon/stale", cwd=repo)
    git("merge", "main", cwd=repo)

    assert inspect_branch(repo, "devon/stale", repo).last_commit_at is None


def test_a_tracked_branch_with_no_local_branch_is_held_back_not_a_crash():
    r = rig(gone=PrSnapshot(status(), None))
    r.state = BranchState(
        exists=False, head_sha="", last_commit_at=None, clean=False, busy=False
    )

    r.tick()

    assert r.decision("gone").reason.code == "no_worktree"
    assert r.launched == []


def test_inspect_branch_says_when_the_local_branch_does_not_exist(repo: Path):
    assert inspect_branch(repo, "devon/nope", None).exists is False


def test_a_failing_pass_is_reported_and_the_loop_carries_on():
    r = rig(a=PrSnapshot(status(), None))
    passes = {"n": 0}
    real = r.dispatcher.tick

    async def flaky() -> None:
        passes["n"] += 1
        if passes["n"] == 1:
            raise RuntimeError("gh is down")
        await real()

    r.dispatcher.tick = flaky  # type: ignore[method-assign]

    async def scenario() -> None:
        ticks = asyncio.Event()

        async def sleep(_seconds: float) -> None:
            await ticks.wait()
            ticks.clear()

        r.dispatcher._sleep = sleep
        task = asyncio.create_task(r.dispatcher.run())
        for _ in range(20):
            await asyncio.sleep(0)
        assert r.dispatcher.last_error == "RuntimeError: gh is down"
        ticks.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if r.dispatcher.ticked_at:
                break
        assert r.dispatcher.last_error is None
        assert r.decision("a") is not None
        task.cancel()

    asyncio.run(scenario())


def test_with_the_start_switch_off_tasks_are_decided_but_nothing_is_launched():
    r = rig(a=PrSnapshot(status(), None))
    r.launching = False

    r.tick()

    assert r.decision("a").may_start
    assert r.launched == []
    assert r.dispatcher.dry_run is True


def test_with_the_start_switch_on_it_is_not_a_dry_run():
    r = rig(a=PrSnapshot(status(), None))

    r.tick()

    assert r.dispatcher.dry_run is False


def test_only_one_run_is_started_per_pass():
    r = rig(
        a=PrSnapshot(status(), None),
        b=PrSnapshot(status(), None),
        c=PrSnapshot(status(), None),
    )

    r.tick()

    assert r.launched == ["a"]
