"""An autonomous run that asks the user a question: it ends as `deciding`, waits
in the database, and is resumed with the answer once the user has given it."""

import asyncio
from datetime import timedelta
from pathlib import Path

from test_agent_runner import NOW, Rig, assistant, decision, result
from test_dispatcher import PrSnapshot, holder, status

from toolbelt.autonomous_agents.eligibility import TaskFacts, World, resume_blocker
from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.autonomous_agents.questions import RunQuestions
from toolbelt.autonomous_agents.runner import ALLOWED_TOOLS, DECISIONS_SERVER
from toolbelt.decisions.book import DecisionBook
from toolbelt.decisions.model import points_from_data
from toolbelt.git.stack.store import StackStore, TokenCounts, default_db_path
from toolbelt.task_claims.resolution import TrackedTask

POINTS = points_from_data(
    [
        {
            "id": "D1",
            "title": "Which fix?",
            "options": [
                {"id": "O1", "title": "Patch"},
                {"id": "O2", "title": "Rewrite"},
            ],
        }
    ]
)
ANSWER = [{"point_id": "D1", "option_id": "O2", "text": "carefully"}]
TASK = TrackedTask(1, "api", "devon/a", "t1", None, Path("/wt/api/a"))


def book_of(rig: Rig) -> DecisionBook:
    return DecisionBook(open_store=lambda: StackStore(rig.db), wall_clock=lambda: NOW)


class TestRunnerAsking:
    def test_a_run_has_the_tools_to_ask_and_a_new_session(self, tmp_path: Path):
        rig = Rig(tmp_path, [assistant(), result()])

        rig.run()

        (call,) = rig.calls
        options = call["options"]
        assert f"mcp__{DECISIONS_SERVER}__propose_decisions" in ALLOWED_TOOLS
        assert set(options.mcp_servers) == {DECISIONS_SERVER}
        assert options.session_id is not None and options.resume is None

    def test_a_run_that_asked_and_stopped_ends_as_deciding_and_releases_its_task(
        self, tmp_path: Path
    ):
        asked = {}

        async def query(*, prompt, options):
            (running,) = [r for r in rig.runs_now() if r.state == "running"]
            asked["decision"] = book_of(rig).propose(
                TASK,
                proposer="autonomous",
                title="Fix",
                points=POINTS,
                session_id=running.session_id,
                run_id=running.run_id,
            )
            yield assistant()
            yield result()

        rig = Rig(tmp_path)
        rig.runner._query = query

        run = rig.run()

        assert (run.state, run.error) == ("deciding", None)
        assert run.ended_at is not None
        claim = rig.read_claim()
        assert (
            claim is not None and claim["claims"] == []
        )  # it holds nothing while it waits
        assert asked["decision"].run_id == run.run_id

    def test_a_run_that_did_not_ask_is_not_deciding(self, tmp_path: Path):
        assert Rig(tmp_path, [assistant(), result()]).run().state == "succeeded"

    def test_an_answered_question_does_not_make_a_later_run_deciding(
        self, tmp_path: Path
    ):
        rig = Rig(tmp_path, [assistant(), result()])
        old = book_of(rig).propose(
            TASK, proposer="autonomous", title="Old", points=POINTS, run_id="earlier"
        )
        book_of(rig).answer(old.id, ANSWER)

        assert rig.run().state == "succeeded"


class TestResuming:
    def deciding(self, tmp_path: Path):
        """A run that asked, and the user's answer to it."""
        rig = Rig(tmp_path, [assistant(), result(session_id="resumed-session")])
        store = StackStore(rig.db)
        run_id = store.start_agent_run(
            task_id="t1",
            repo_id=1,
            branch="devon/a",
            trigger="resolve_comments",
            head_sha="abc",
            model="m",
            at=NOW - timedelta(hours=5),
            session_id="asking-session",
        )
        store.finish_agent_run(
            run_id,
            state="deciding",
            ended_at=NOW - timedelta(hours=5),
            session_id="asking-session",
            cost_usd=0.05,
            tokens=TokenCounts(),
        )
        prior = store.agent_run(run_id)
        store.close()
        asked = book_of(rig).propose(
            TASK,
            proposer="autonomous",
            title="Fix",
            points=POINTS,
            session_id="asking-session",
            run_id=run_id,
        )
        answered = book_of(rig).answer(asked.id, ANSWER)
        return rig, prior, answered

    def resume(self, rig: Rig, answered, prior):
        async def scenario():
            run_id = await rig.runner.resume(answered, decision("fix_ci"), prior)
            await rig.runner.running[run_id]
            return run_id

        return asyncio.run(scenario())

    def test_the_run_is_resumed_in_its_session_with_the_answer(self, tmp_path: Path):
        rig, prior, answered = self.deciding(tmp_path)

        run_id = self.resume(rig, answered, prior)

        (call,) = rig.calls
        assert call["options"].resume == "asking-session"
        assert call["options"].session_id is None
        assert "Chosen: O2 (Rewrite)" in call["prompt"]
        assert "In the user's words: carefully" in call["prompt"]
        runs = {r.run_id: r for r in rig.runs_now()}
        resumed = runs[run_id]
        assert resumed.resumed_from == prior.run_id
        assert resumed.trigger == "resolve_comments"  # the job it asked about
        assert resumed.state == "succeeded"

    def test_the_earlier_run_is_settled_and_the_answer_is_delivered(
        self, tmp_path: Path
    ):
        rig, prior, answered = self.deciding(tmp_path)

        self.resume(rig, answered, prior)

        runs = {r.run_id: r for r in rig.runs_now()}
        assert runs[prior.run_id].state == "succeeded"
        assert book_of(rig).get(answered.id).delivered_at is not None

    def test_a_resumed_run_can_ask_again(self, tmp_path: Path):
        rig, prior, answered = self.deciding(tmp_path)

        async def query(*, prompt, options):
            (running,) = [r for r in rig.runs_now() if r.state == "running"]
            book_of(rig).propose(
                TASK,
                proposer="autonomous",
                title="Again",
                points=POINTS,
                run_id=running.run_id,
            )
            yield result()

        rig.runner._query = query

        run_id = self.resume(rig, answered, prior)

        assert {r.run_id: r for r in rig.runs_now()}[run_id].state == "deciding"


def facts(**changes) -> TaskFacts:
    base = {
        "task_id": "t1",
        "action_kind": "fix_ci",
        "pr_open": True,
        "pr_draft": False,
        "head_sha": "abc",
        "last_commit_at": NOW - timedelta(days=30),
        "has_worktree": True,
        "worktree_clean": True,
        "worktree_busy": False,
        "claimed_by_other": False,
        "bot_reviews": 9,
        "failed_ci_commits": 9,
    }
    return TaskFacts(**{**base, **changes})


def world(**changes) -> World:
    base = {
        "now": NOW,
        "enabled": True,
        "limits": AgentLimits(),
        "screen": None,
        "auth_ok": True,
        "runs": [],
    }
    return World(**{**base, **changes})


class TestResumeRules:
    def test_a_run_may_be_resumed_past_what_stops_a_new_attempt(self):
        # A draft-era branch, long without commits, thrashing, with a dirty tree:
        # it continues a job already taken.
        assert (
            resume_blocker(facts(pr_draft=True, worktree_clean=False), world()) is None
        )

    def test_what_is_unsafe_or_impossible_still_stops_it(self):
        assert resume_blocker(facts(pr_open=False), world()).code == "pr_not_open"  # type: ignore[union-attr]
        assert resume_blocker(facts(claimed_by_other=True), world()).code == "claimed"  # type: ignore[union-attr]
        assert resume_blocker(facts(has_worktree=False), world()).code == "no_worktree"  # type: ignore[union-attr]
        assert (
            resume_blocker(facts(worktree_busy=True), world()).code == "worktree_busy"
        )  # type: ignore[union-attr]
        assert resume_blocker(facts(), world(enabled=False)).code == "disabled"  # type: ignore[union-attr]
        assert resume_blocker(facts(), world(auth_ok=False)).code == "auth"  # type: ignore[union-attr]


def seed_deciding_run(branch: str = "a") -> str:
    store = StackStore(default_db_path())
    try:
        run_id = store.start_agent_run(
            task_id=f"id-{branch}",
            repo_id=1,
            branch=branch,
            trigger="fix_ci",
            head_sha="abc",
            model="m",
            at=NOW - timedelta(hours=3),
            session_id="s",
        )
        store.finish_agent_run(
            run_id,
            state="deciding",
            ended_at=NOW - timedelta(hours=3),
            session_id="s",
            tokens=TokenCounts(),
        )
        return run_id
    finally:
        store.close()


def ask(run_id: str, branch: str = "a", *, answered: bool = False):
    task = TrackedTask(1, "api", branch, f"id-{branch}", None, Path(f"/wt/{branch}"))
    book = DecisionBook(wall_clock=lambda: NOW)
    asked = book.propose(
        task,
        proposer="autonomous",
        title="Fix",
        points=POINTS,
        run_id=run_id,
        session_id="s",
    )
    return book.answer(asked.id, ANSWER) if answered else asked


class TestDispatching:
    def rig(self, resumed: list, **snapshots):
        from test_dispatcher import rig

        r = rig(**snapshots)
        r.dispatcher._questions = RunQuestions(lambda: StackStore(default_db_path()))

        async def resume(answered, decision, prior):
            resumed.append((answered.id, decision.branch, prior.run_id))

        r.dispatcher._resume = resume
        return r

    def test_a_task_with_an_unanswered_question_is_held_and_not_launched(self):
        resumed: list = []
        r = self.rig(resumed, a=PrSnapshot(status(), None))
        ask(seed_deciding_run())

        r.tick()

        assert r.decision("a").reason.code == "deciding"  # type: ignore[union-attr]
        assert r.launched == [] and resumed == []

    def test_an_answered_question_resumes_its_run_instead_of_starting_a_new_one(self):
        resumed: list = []
        r = self.rig(resumed, a=PrSnapshot(status(), None))
        run_id = seed_deciding_run()
        asked = ask(run_id, answered=True)

        r.tick()

        assert resumed == [(asked.id, "a", run_id)]
        assert r.launched == []  # still held: nothing else starts on the task

    def test_it_waits_while_a_person_holds_the_task(self):
        resumed: list = []
        r = self.rig(resumed, a=PrSnapshot(status(), None))
        r.holders["a"] = (holder("agent", "idle"),)
        ask(seed_deciding_run(), answered=True)

        r.tick()

        assert resumed == []

    def test_nothing_is_resumed_while_starting_runs_is_off(self):
        resumed: list = []
        r = self.rig(resumed, a=PrSnapshot(status(), None))
        r.launching = False
        ask(seed_deciding_run(), answered=True)

        r.tick()

        assert resumed == []

    def test_a_run_whose_question_was_deleted_is_settled_as_stopped(self):
        resumed: list = []
        r = self.rig(resumed, a=PrSnapshot(status(), None))
        run_id = seed_deciding_run()
        DecisionBook().delete(ask(run_id).id)

        r.tick()

        store = StackStore(default_db_path())
        try:
            run = store.agent_run(run_id)
        finally:
            store.close()
        assert run is not None
        assert (run.state, run.error) == ("stopped", "its question was deleted")
        assert resumed == []
