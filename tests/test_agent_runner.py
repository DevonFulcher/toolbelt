"""One autonomous run: the run record, the claim file, the limits, and how it
ends. The SDK's `query` is a fake that yields real SDK message objects."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage

from toolbelt.autonomous_agents.dispatcher import Decision
from toolbelt.autonomous_agents.instructions import (
    AI_PREFIX,
    build_instructions,
    build_prompt,
)
from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.autonomous_agents.runner import AgentRunner
from toolbelt.git.stack.store import StackStore
from toolbelt.task_claims.claim_file import parse_claim_file

NOW = datetime(2026, 10, 13, 17, 0, tzinfo=UTC)
LIMITS = AgentLimits(run_timeout_seconds=1)


def decision(trigger: str = "fix_ci", **changes) -> Decision:
    base = {
        "task_id": "t1",
        "repo_id": 1,
        "repo": "api",
        "branch": "devon/a",
        "trigger": trigger,
        "reason": None,
        "head_sha": "abc",
        "decided_at": NOW,
        "pr_number": 7,
        "pr_url": "https://github.com/o/api/pull/7",
        "base_ref": "main",
        "approved": False,
        "worktree": Path("/wt/api/a"),
    }
    return Decision(**{**base, **changes})


USAGE = {"input_tokens": 3, "output_tokens": 400, "cache_read_input_tokens": 50_000}


def assistant(message_id: str = "m1", usage: dict | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        model="claude-haiku-5-5",
        usage=usage or USAGE,
        message_id=message_id,
    )


def result(**changes) -> ResultMessage:
    base = {
        "subtype": "success",
        "duration_ms": 1000,
        "duration_api_ms": 800,
        "is_error": False,
        "num_turns": 6,
        "session_id": "sdk-session",
        "total_cost_usd": 0.0412,
        "usage": USAGE,
        "result": 'Fixed the lint error.\n{"pushed": true}',
    }
    return ResultMessage(**{**base, **changes})


class Rig:
    def __init__(self, tmp_path: Path, messages=(), *, hang=False, raises=None):
        self.db = tmp_path / "stacks.db"
        self.claims = tmp_path / "claims"
        self.calls: list[dict] = []
        self.claim_seen_during: dict | None = None
        self.hang = hang

        async def query(*, prompt: str, options) -> AsyncIterator:
            self.calls.append({"prompt": prompt, "options": options})
            self.claim_seen_during = self.read_claim()
            if raises:
                raise raises
            for message in messages:
                yield message
            if hang:
                await asyncio.sleep(60)

        self.runner = AgentRunner(
            db_path=lambda: self.db,
            claims_dir=lambda: self.claims,
            limits=LIMITS,
            wall_clock=lambda: NOW,
            query=query,
            heartbeat_seconds=0.05,
        )

    def read_claim(self) -> dict | None:
        files = list(self.claims.glob("auto.*.json")) if self.claims.exists() else []
        return json.loads(files[0].read_text()) if files else None

    def run(self, d: Decision | None = None):
        async def scenario():
            run_id = await self.runner.start(d or decision())
            await self.runner.running[run_id]
            return run_id

        run_id = asyncio.run(scenario())
        store = StackStore(self.db)
        try:
            (run,) = [r for r in store.agent_runs() if r.run_id == run_id]
        finally:
            store.close()
        return run


def test_a_successful_run_records_what_the_sdk_reports(tmp_path: Path):
    rig = Rig(tmp_path, [assistant(), result()])

    run = rig.run()

    assert (run.state, run.error) == ("succeeded", None)
    assert (run.turns, run.session_id) == (6, "sdk-session")
    assert run.cost_usd == 0.0412
    assert run.tokens.output == 400 and run.tokens.cache_read == 50_000
    assert run.summary and run.summary.startswith("Fixed the lint error.")
    assert (run.task_id, run.trigger, run.head_sha) == ("t1", "fix_ci", "abc")
    assert run.model == LIMITS.model


def test_the_run_holds_a_v2_claim_while_working_and_releases_it_after(tmp_path: Path):
    rig = Rig(tmp_path, [assistant(), result()])

    run = rig.run()

    during = rig.claim_seen_during
    assert during is not None
    assert (during["version"], during["kind"], during["state"]) == (
        2,
        "autonomous",
        "working",
    )
    assert during["holder"] == f"auto.{run.run_id}" and during["run_id"] == run.run_id
    assert during["claims"][0] == {
        "kind": "worktree",
        "path": "/wt/api/a",
        "touched_at": during["claims"][0]["touched_at"],
    }
    after = rig.read_claim()
    assert after is not None and after["ttl_seconds"] == 0 and after["claims"] == []
    # Both files are ones the reader accepts.
    for document in (during, after):
        parsed = parse_claim_file(
            json.dumps(document), expected_holder=f"auto.{run.run_id}"
        )
        assert (parsed.kind, parsed.version) == ("autonomous", 2)


def test_the_sdk_is_given_the_run_limits_the_worktree_and_the_instructions(
    tmp_path: Path,
):
    rig = Rig(tmp_path, [result()])

    rig.run()

    (call,) = rig.calls
    options = call["options"]
    assert options.model == LIMITS.model
    assert options.cwd == "/wt/api/a"
    assert options.max_turns == LIMITS.run_turns
    assert options.max_budget_usd == LIMITS.run_usd
    assert "Task" in options.disallowed_tools
    assert options.setting_sources == ["project"]
    assert options.system_prompt["preset"] == "claude_code"
    assert "fix the failing CI" in call["prompt"]
    assert "devon/a" in options.system_prompt["append"]
    assert options.session_id


def test_a_run_that_hits_its_turn_or_dollar_cap_says_so(tmp_path: Path):
    for subtype in ("error_max_turns", "error_max_budget_usd"):
        rig = Rig(tmp_path, [result(subtype=subtype, is_error=True)])

        run = rig.run()

        assert (run.state, run.error) == ("cap_reached", subtype)


def test_an_error_result_is_a_failed_run_with_its_text(tmp_path: Path):
    rig = Rig(
        tmp_path,
        [result(subtype="error_during_execution", is_error=True, result="boom")],
    )

    run = rig.run()

    assert (run.state, run.error) == ("failed", "boom")


def test_a_sign_in_failure_says_to_log_in(tmp_path: Path):
    rig = Rig(
        tmp_path,
        [
            result(
                subtype="error_during_execution",
                is_error=True,
                result="Failed to authenticate: OAuth session expired",
            )
        ],
    )

    run = rig.run()

    assert run.state == "failed"
    assert "claude auth login" in (run.error or "")


def test_an_exception_from_the_sdk_is_a_failed_run_not_a_crash(tmp_path: Path):
    rig = Rig(tmp_path, raises=RuntimeError("cli not found"))

    run = rig.run()

    assert (run.state, run.error) == ("failed", "RuntimeError: cli not found")
    claim = rig.read_claim()
    assert claim is not None and claim["ttl_seconds"] == 0


def test_a_run_past_the_time_limit_is_timed_out_and_keeps_what_it_spent(tmp_path: Path):
    rig = Rig(tmp_path, [assistant("m1"), assistant("m1"), assistant("m2")], hang=True)

    run = rig.run()

    assert run.state == "timed_out"
    assert "minutes" in (run.error or "")
    assert run.turns == 2
    assert run.cost_usd > 0
    assert run.tokens.output == 800


def test_stopping_a_run_ends_it_as_stopped(tmp_path: Path):
    rig = Rig(tmp_path, [assistant()], hang=True)
    rig.runner._limits = AgentLimits(run_timeout_seconds=60)

    async def scenario():
        run_id = await rig.runner.start(decision())
        for _ in range(20):
            await asyncio.sleep(0.01)
        task = rig.runner.running[run_id]
        assert rig.runner.stop(run_id) is True
        await task
        return run_id

    run_id = asyncio.run(scenario())
    store = StackStore(rig.db)
    (run,) = [r for r in store.agent_runs() if r.run_id == run_id]
    store.close()

    assert run.state == "stopped"
    assert rig.runner.stop("nope") is False


def test_a_run_ending_without_a_result_is_failed(tmp_path: Path):
    rig = Rig(tmp_path, [assistant()])

    run = rig.run()

    assert (run.state, run.error) == ("failed", "the run ended without a result")


def test_a_decision_without_a_worktree_cannot_start(tmp_path: Path):
    rig = Rig(tmp_path, [result()])

    async def scenario():
        await rig.runner.start(decision(worktree=None))

    try:
        asyncio.run(scenario())
    except ValueError as error:
        assert "worktree" in str(error)
    else:
        raise AssertionError("expected a ValueError")


# ---- the instructions ------------------------------------------------------------------------------


def test_instructions_carry_the_pr_the_worktree_and_the_ground_rules():
    text = build_instructions(decision(), LIMITS)

    assert "https://github.com/o/api/pull/7" in text
    assert "/wt/api/a" in text
    assert AI_PREFIX in text and "Co-Authored-By: AI Assistant" in text
    assert "Never force-push" in text
    assert 'toolbelt git save -m "<message>"' in text
    assert "never plain `git`" in text and "`needs_human`" in text
    assert (
        "15 minutes" not in text and "about 0 minutes" in text
    )  # LIMITS has a 1 s limit


def test_where_a_fix_goes_depends_on_the_prs_state():
    ready = build_instructions(decision(approved=False), LIMITS)
    approved = build_instructions(decision(approved=True), LIMITS)

    assert "draft has no reviewers" not in ready
    assert "not approved yet: commit a non-critical fix" in ready
    assert "however small, is stacked" in approved


def test_each_trigger_has_its_own_task_and_prompt():
    conflicts = build_instructions(decision("resolve_conflicts"), LIMITS)
    ci = build_instructions(decision("fix_ci"), LIMITS)
    comments = build_instructions(decision("resolve_comments"), LIMITS)

    assert "toolbelt git sync" in conflicts and "git merge --abort" in conflicts
    assert "flake" in ci and "CODEOWNERS" in ci and "DRAFT PR" in ci
    assert "NEVER resolve a thread a human started" in comments
    assert "resolve the merge conflicts" in build_prompt(decision("resolve_conflicts"))


def test_a_report_with_a_summary_is_recorded_as_that_summary(tmp_path: Path):
    message = (
        "Long account of everything.\n"
        '{"summary": "Fixed the lint error and pushed.", "pushed": true}'
    )
    rig = Rig(tmp_path, [assistant(), result(result=message)])

    assert rig.run().summary == "Fixed the lint error and pushed."
