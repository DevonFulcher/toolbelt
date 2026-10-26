"""The worker that takes up approved proposals, and what an implement run is
told and allowed."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_agent_eligibility import task, world
from test_proposals import content_data

from toolbelt.autonomous_agents.dispatcher import Decision
from toolbelt.autonomous_agents.eligibility import Reason, resume_blocker
from toolbelt.autonomous_agents.implement_setup import (
    SetupFailed,
    branch_name_for,
    brief_of,
)
from toolbelt.autonomous_agents.instructions import build_instructions, build_prompt
from toolbelt.autonomous_agents.limits import IMPLEMENT, AgentLimits
from toolbelt.autonomous_agents.runner import (
    IMPLEMENT_TOOLS,
    AgentRunner,
)
from toolbelt.git.stack.store import RepoIdentity, StackStore
from toolbelt.proposals.book import ProposalBook
from toolbelt.proposals.model import content_from_data
from toolbelt.proposals.settings import ProposalSettings
from toolbelt.ui.proposal_worker import ProposalWorker, RepoRef

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        source: str = "jira",
        source_ref: str = "TB-9",
        repo: str = "toolbelt",
        settings: ProposalSettings | None = None,
        blockers: list[Reason] | None = None,
        problem: str | None = None,
    ):
        self.db = tmp_path / "stacks.db"
        checkout = tmp_path / "toolbelt"
        checkout.mkdir()
        store = StackStore(self.db)
        self.repo_id = store.ensure_repo(
            RepoIdentity(
                git_common_dir=checkout / ".git",
                path=checkout,
                remote_url="git@github.com:o/toolbelt.git",
            )
        )
        store.set_parent(self.repo_id, "devon/the-branch", "main")
        store.close()
        self.book = ProposalBook(
            open_store=lambda: StackStore(self.db), wall_clock=lambda: NOW
        )
        data = {**content_data(), "repo": repo}
        (self.proposal,) = self.book.propose(
            [(source, source_ref, "https://x/1", content_from_data(data))], run_id=None
        )
        self.book.approve(self.proposal.id)
        self.started: list[tuple[Decision, str]] = []
        self.tickets: list[tuple[str, str]] = []
        self.appended: list[str] = []

        async def create_ticket(proposal, project: str) -> str:
            self.tickets.append((proposal.id, project))
            return "NEW-1"

        def append_branch(path: Path, proposal, task_key: str):
            self.appended.append(task_key)
            return tmp_path / "wt", "devon/the-branch"

        async def start(decision: Decision, proposal_id: str) -> str:
            self.started.append((decision, proposal_id))
            return "run-1"

        self.worker = ProposalWorker(
            book=self.book,
            db_path=lambda: self.db,
            settings=settings or ProposalSettings(),
            repos=lambda: [RepoRef(self.repo_id, "toolbelt", "o/toolbelt", checkout)],
            blockers=lambda: blockers or [],
            launching=lambda: True,
            checkout_problem=lambda path: problem,
            create_ticket=create_ticket,
            append_branch=append_branch,
            head_sha=lambda worktree: "abc123",
            start=start,
            wall_clock=lambda: NOW,
        )

    def now(self):
        return self.book.get(self.proposal.id)


def step(rig: Rig) -> None:
    asyncio.run(rig.worker.step())


def test_an_approved_jira_proposal_gets_a_branch_and_an_implement_run(tmp_path):
    rig = Rig(tmp_path)

    step(rig)

    ((decision, proposal_id),) = rig.started
    assert proposal_id == rig.proposal.id
    assert (decision.trigger, decision.branch, decision.base_ref) == (
        IMPLEMENT,
        "devon/the-branch",
        "main",
    )
    assert "Retry Jira fetch on 429" in (decision.brief or "")
    assert rig.appended == ["TB-9"]
    after = rig.now()
    assert (after.task_key, after.branch, after.worker_run_id) == (
        "TB-9",
        "devon/the-branch",
        "run-1",
    )


def test_a_proposal_from_elsewhere_gets_a_ticket_in_the_configured_project(tmp_path):
    rig = Rig(
        tmp_path,
        source="sentry",
        source_ref="APP-3F2",
        settings=ProposalSettings(jira_project="TB"),
    )

    step(rig)

    assert rig.tickets == [(rig.proposal.id, "TB")]
    assert rig.appended == ["NEW-1"]
    assert rig.now().task_key == "NEW-1"


def test_a_proposal_from_elsewhere_without_a_jira_project_is_left_with_a_note(tmp_path):
    rig = Rig(tmp_path, source="sentry", source_ref="APP-3F2")

    step(rig)

    assert rig.started == []
    assert "proposals.jira_project" in rig.now().work_note


def test_a_proposal_for_an_unknown_repo_is_left_with_a_note(tmp_path):
    rig = Rig(tmp_path, repo="elsewhere")

    step(rig)

    assert rig.started == []
    assert "no tracked repo" in rig.now().work_note


def test_a_busy_checkout_is_waited_on_without_a_note(tmp_path):
    rig = Rig(tmp_path, problem="the checkout has uncommitted changes")

    step(rig)

    assert rig.started == [] and rig.appended == []
    assert rig.now().work_note == ""


def test_nothing_starts_while_the_agents_are_blocked(tmp_path):
    rig = Rig(tmp_path, blockers=[Reason("disabled", "off")])

    step(rig)

    assert rig.started == []


def test_a_failed_setup_is_noted_and_not_repeated(tmp_path):
    rig = Rig(tmp_path)

    def fail(path, proposal, task_key):
        raise SetupFailed("toolbelt git append failed: boom")

    rig.worker._append_branch = fail
    step(rig)
    step(rig)

    assert rig.now().work_note == "toolbelt git append failed: boom"
    assert rig.started == []


def test_the_branch_name_comes_from_the_title(tmp_path):
    rig = Rig(tmp_path)

    assert branch_name_for(rig.proposal) == "retry-jira-fetch-on-429"


def test_an_implement_run_is_told_to_ask_through_decisions_and_stop_at_a_branch():
    decision = Decision(
        task_id="t",
        repo_id=1,
        repo="toolbelt",
        branch="devon/b",
        trigger=IMPLEMENT,
        reason=None,
        head_sha="abc",
        decided_at=NOW,
        worktree=Path("/wt/b"),
        brief="Title: do the thing",
    )

    text = build_instructions(decision, AgentLimits())

    assert "propose_decisions" in text
    assert "Do not open a pull request" in text
    assert "Title: do the thing" in text
    assert "implement the approved proposal" in build_prompt(decision)


def test_an_implement_run_uses_the_users_settings_and_its_own_limits():
    limits = AgentLimits()
    runner = AgentRunner(
        db_path=lambda: Path("/nowhere"),
        claims_dir=lambda: Path("/nowhere"),
        limits=limits,
        wall_clock=lambda: NOW,
    )
    decision = Decision(
        task_id="t",
        repo_id=1,
        repo="toolbelt",
        branch="devon/b",
        trigger=IMPLEMENT,
        reason=None,
        head_sha="abc",
        decided_at=NOW,
        worktree=Path("/wt/b"),
    )

    options = runner._options(decision, "session")
    other = runner._options(replace(decision, trigger="fix_ci"), "session")

    assert options.setting_sources == ["user", "project"]
    assert options.max_budget_usd == limits.implement_run_usd
    assert options.model == limits.implement_model
    assert options.allowed_tools == IMPLEMENT_TOOLS
    assert other.setting_sources == ["project"]
    assert other.max_budget_usd == limits.run_usd


def test_an_implement_run_may_resume_on_a_branch_with_no_pr():
    facts = task(action_kind=IMPLEMENT, pr_open=False)

    assert resume_blocker(facts, world()) is None
    assert resume_blocker(replace(facts, action_kind="fix_ci"), world()) is not None


def test_the_brief_carries_what_the_worker_needs(tmp_path):
    rig = Rig(tmp_path)

    brief = brief_of(rig.proposal)

    assert "Done when:" in brief and "429 from Jira" in brief


@pytest.mark.parametrize("key", ["TB-9", "ABC2-123"])
def test_task_keys_are_recognised(key):
    from toolbelt.autonomous_agents.implement_setup import is_task_key

    assert is_task_key(key)
