"""Native agent sources normalize into the shared interactive usage ledger."""

import json
from datetime import UTC, datetime
from pathlib import Path

from toolbelt.agent_providers import providers_for
from toolbelt.agent_providers.claude import ClaudeAgentProvider
from toolbelt.agent_providers.codex import CodexAgentProvider, codex_token_counts
from toolbelt.git.stack.store import StackStore, TokenCounts, UsageCall
from toolbelt.token_spend.ingest import ingest_providers


def test_codex_usage_counts_cached_input_separately() -> None:
    assert codex_token_counts(
        {
            "input_tokens": 120,
            "input_tokens_details": {"cached_tokens": 45},
            "output_tokens": 30,
        }
    ) == TokenCounts(input=75, output=30, cache_read=45)


def test_codex_usage_provider_reads_hook_events_into_shared_ledger(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    usage_dir = state / "usage" / "codex"
    usage_dir.mkdir(parents=True)
    event = {
        "version": 1,
        "event_id": "response-1",
        "session_id": "session-1",
        "agent_id": None,
        "turn_id": "turn-1",
        "model": "gpt-5-codex",
        "at": datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
        "cwd": str(tmp_path),
        "token_usage": {
            "input_tokens": 120,
            "input_tokens_details": {"cached_tokens": 45},
            "output_tokens": 30,
        },
    }
    (usage_dir / "session-1.jsonl").write_text(json.dumps(event) + "\n")
    db = state / "stacks.db"
    store = StackStore(db)
    try:
        providers = providers_for(
            claude_projects=tmp_path / "claude-projects",
            state_directory=state,
        )
        result = ingest_providers(store, providers)
        assert result.new_calls == 1
        assert ingest_providers(store, providers).new_calls == 0
        row = store._conn.execute(
            "SELECT provider, session_id, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd FROM usage_calls"
        ).fetchone()
        assert tuple(row)[:5] == ("codex", "session-1", 75, 30, 45)
        assert row["cost_usd"] > 0
    finally:
        store.close()


def test_codex_usage_parser_skips_invalid_events(tmp_path: Path) -> None:
    provider = CodexAgentProvider(tmp_path)
    calls, unreadable = provider.parse_usage(["not json\n", "{}\n"])

    assert calls == []
    assert unreadable == 2


def test_claude_compatibility_ingest_ignores_unpriced_codex_calls(
    tmp_path: Path,
) -> None:
    store = StackStore(tmp_path / "stacks.db")
    try:
        store.add_usage_calls(
            [
                UsageCall(
                    message_id="codex:unknown-model",
                    session_id="session-1",
                    agent_id=None,
                    model="unknown-model",
                    at=datetime(2026, 10, 1, tzinfo=UTC),
                    cwd=None,
                    tokens=TokenCounts(input=10, output=5),
                    usage={},
                    cost_usd=None,
                    provider="codex",
                )
            ],
            transcript="codex.jsonl",
            offset=1,
        )

        ingest_providers(store, [ClaudeAgentProvider(tmp_path / "claude")])

        assert store.unpriced_usage("unknown-model")[0].provider == "codex"
    finally:
        store.close()
