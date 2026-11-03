import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hooks.toolbelt_hook import (
    ACTIVE_TTL_SECONDS,
    CodexHook,
    prompt_context,
)


class CodexHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "toolbelt"
        self.state = Path(self.temporary.name) / "plugin-data"
        self.hook = CodexHook(self.root, self.state)
        self.start = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        self.old_projects_dir = os.environ.get("GIT_PROJECTS_WORKDIR")
        os.environ["GIT_PROJECTS_WORKDIR"] = "/work"

    def tearDown(self) -> None:
        if self.old_projects_dir is None:
            os.environ.pop("GIT_PROJECTS_WORKDIR", None)
        else:
            os.environ["GIT_PROJECTS_WORKDIR"] = self.old_projects_dir
        self.temporary.cleanup()

    def event(self, name: str, **fields: object) -> dict[str, object]:
        return {
            "hook_event_name": name,
            "session_id": "thread-123",
            "turn_id": "turn-456",
            "cwd": "/repo",
            **fields,
        }

    def read_json(self, path: Path) -> dict[str, object]:
        return json.loads(path.read_text())

    def test_session_start_writes_a_codex_claim_with_active_ttl(self) -> None:
        self.hook.handle(self.event("SessionStart"), now=self.start)

        claim = self.read_json(self.root / "claims" / "thread-123.json")
        self.assertEqual(claim["version"], 1)
        self.assertEqual(claim["link"], "codex://threads/thread-123")
        self.assertEqual(claim["state"], "working")
        self.assertEqual(claim["ttl_seconds"], ACTIVE_TTL_SECONDS)
        self.assertEqual(claim["started_at"], "2026-11-03T12:00:00Z")

    def test_pre_tool_use_claims_the_worktree(self) -> None:
        self.hook.handle(
            self.event(
                "PreToolUse",
                cwd="/work/wt/repo/feature",
                tool_name="Bash",
                tool_use_id="tool-1",
                tool_input={"command": "pytest tests/test_a.py"},
            ),
            now=self.start,
        )

        claim = self.read_json(self.root / "claims" / "thread-123.json")
        self.assertEqual(
            claim["claims"],
            [
                {
                    "kind": "worktree",
                    "path": "/work/wt/repo/feature",
                    "touched_at": "2026-11-03T12:00:00Z",
                }
            ],
        )

    def test_tool_events_write_timed_activity_without_persisting_response_or_args(
        self,
    ) -> None:
        self.hook.handle(
            self.event(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id="tool-1",
                tool_input={"command": "gh pr create --body secret-value"},
            ),
            now=self.start,
        )
        self.hook.handle(
            self.event(
                "PostToolUse",
                tool_name="Bash",
                tool_use_id="tool-1",
                tool_response={"exit_code": 0, "stdout": "do-not-store-this"},
            ),
            now=self.start + timedelta(seconds=2),
        )

        chunk = self.read_json(self.root / "activity" / "thread-123.turn-456.1.json")
        self.assertEqual(chunk["final"], False)
        self.assertEqual(chunk["calls"][0]["duration_ms"], 2000)
        self.assertEqual(chunk["calls"][0]["detail"], "gh pr")
        self.assertFalse(chunk["calls"][0]["background"])
        claim = self.read_json(self.root / "claims" / "thread-123.json")
        self.assertEqual(claim["activity"]["open"], False)
        persisted = json.dumps([chunk, claim])
        self.assertNotIn("secret-value", persisted)
        self.assertNotIn("do-not-store-this", persisted)

    def test_model_usage_is_written_as_raw_codex_usage(self) -> None:
        usage = {"input_tokens": 12, "output_tokens": 4, "provider_field": "kept"}
        self.hook.handle(
            self.event(
                "PostModelRequest",
                model="gpt-6.1-sol",
                token_usage=usage,
                agent_id="agent-1",
            ),
            now=self.start,
        )

        lines = (
            (self.root / "usage" / "codex" / "thread-123.jsonl")
            .read_text()
            .splitlines()
        )
        row = json.loads(lines[0])
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["session_id"], "thread-123")
        self.assertEqual(row["agent_id"], "agent-1")
        self.assertEqual(row["turn_id"], "turn-456")
        self.assertEqual(row["model"], "gpt-6.1-sol")
        self.assertEqual(row["token_usage"], usage)
        self.assertTrue(row["event_id"])

    def test_user_prompt_context_supplies_the_explicit_session_id(self) -> None:
        context = prompt_context("thread-123")
        self.assertEqual(
            context["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit"
        )
        self.assertIn(
            "session_id=thread-123", context["hookSpecificOutput"]["additionalContext"]
        )
        self.assertIn(
            "codex://threads/thread-123",
            context["hookSpecificOutput"]["additionalContext"],
        )
        self.assertIn(
            "pass this session_id explicitly",
            context["hookSpecificOutput"]["additionalContext"],
        )

    def test_stop_closes_turn_and_marks_claim_idle(self) -> None:
        self.hook.handle(self.event("UserPromptSubmit"), now=self.start)
        self.hook.handle(
            self.event(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id="tool-1",
                tool_input={"command": "pytest -q tests/test_a.py"},
            ),
            now=self.start + timedelta(seconds=1),
        )
        self.hook.handle(
            self.event("PostToolUse", tool_name="Bash", tool_use_id="tool-1"),
            now=self.start + timedelta(seconds=3),
        )
        self.hook.handle(self.event("Stop"), now=self.start + timedelta(seconds=5))

        final = self.read_json(self.root / "activity" / "thread-123.turn-456.2.json")
        self.assertTrue(final["final"])
        self.assertEqual(final["totals"]["wall_ms"], 5000)
        self.assertEqual(final["totals"]["tool_ms"], 2000)
        self.assertEqual(final["totals"]["model_ms"], 3000)
        claim = self.read_json(self.root / "claims" / "thread-123.json")
        self.assertEqual(claim["state"], "idle")
        self.assertEqual(claim["ttl_seconds"], ACTIVE_TTL_SECONDS)

    def test_session_end_releases_claim_and_closes_any_open_turn(self) -> None:
        self.hook.handle(self.event("UserPromptSubmit"), now=self.start)
        self.hook.handle(
            self.event(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id="tool-1",
                tool_input={"command": "pytest tests/test_a.py"},
            ),
            now=self.start + timedelta(seconds=1),
        )
        self.hook.handle(
            self.event("SessionEnd"), now=self.start + timedelta(seconds=3)
        )

        final = self.read_json(self.root / "activity" / "thread-123.turn-456.1.json")
        self.assertTrue(final["final"])
        self.assertTrue(final["calls"][0]["unended"])
        claim = self.read_json(self.root / "claims" / "thread-123.json")
        self.assertEqual(claim["state"], "idle")
        self.assertEqual(claim["ttl_seconds"], 0)
        self.assertEqual(claim["claims"], [])

    def test_parallel_calls_count_as_the_union_of_tool_time(self) -> None:
        for tool_use_id, seconds in (("one", 0), ("two", 1)):
            self.hook.handle(
                self.event(
                    "PreToolUse",
                    tool_name="Bash",
                    tool_use_id=tool_use_id,
                    tool_input={"command": "pytest"},
                ),
                now=self.start + timedelta(seconds=seconds),
            )
        for tool_use_id, seconds in (("one", 3), ("two", 4)):
            self.hook.handle(
                self.event("PostToolUse", tool_name="Bash", tool_use_id=tool_use_id),
                now=self.start + timedelta(seconds=seconds),
            )
        self.hook.handle(self.event("Stop"), now=self.start + timedelta(seconds=5))

        final = self.read_json(self.root / "activity" / "thread-123.turn-456.3.json")
        self.assertEqual(final["totals"]["wall_ms"], 5000)
        self.assertEqual(final["totals"]["tool_ms"], 4000)
        self.assertEqual(final["totals"]["model_ms"], 1000)

    def test_revocation_survives_a_hook_event_that_does_not_touch_the_target(
        self,
    ) -> None:
        self.hook.handle(
            self.event(
                "PreToolUse",
                cwd="/work/wt/repo/branch",
                tool_name="Bash",
                tool_use_id="tool-1",
                tool_input={"command": "pytest tests/test_a.py"},
            ),
            now=self.start,
        )
        (self.root / "claims" / "revoked").mkdir(parents=True)
        (self.root / "claims" / "revoked" / "thread-123.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "holder": "thread-123",
                    "revoked": [
                        {
                            "kind": "worktree",
                            "path": "/work/wt/repo/branch",
                            "at": "2026-11-03T12:00:01Z",
                        }
                    ],
                }
            )
        )
        self.hook.handle(
            self.event("PostModelRequest", model="gpt-6.1-sol", token_usage={}),
            now=self.start + timedelta(seconds=2),
        )

        claim = self.read_json(self.root / "claims" / "thread-123.json")
        self.assertEqual(claim["claims"], [])


if __name__ == "__main__":
    unittest.main()
