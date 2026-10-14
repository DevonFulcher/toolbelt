"""Reads the model calls out of Claude Code's session transcripts.

A transcript is an append-only JSON-lines file: one per session
(``<projects>/<project>/<session>.jsonl``), and one per subagent a session
started (``<projects>/<project>/<session>/subagents/agent-<id>.jsonl``). Each
assistant row carries the API response's id and ``usage``; a response is
written on several rows, so the same id repeats.
"""

import json
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from toolbelt.git.stack.store import TokenCounts, UsageCall


def projects_dir() -> Path:
    """Where Claude Code keeps transcripts: ``$CLAUDE_CONFIG_DIR/projects``,
    default ``~/.claude/projects``."""
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config) if config else Path.home() / ".claude") / "projects"


def transcript_files(projects: Path) -> list[Path]:
    """Every session and subagent transcript under ``projects``."""
    return sorted(
        [*projects.glob("*/*.jsonl"), *projects.glob("*/*/subagents/*.jsonl")]
    )


def tokens_of(usage: dict) -> TokenCounts:
    """The token counts in an API response's ``usage`` object. Cache writes
    are split by lifetime when the response says how; older ones do not and
    count as 5-minute writes."""
    created = usage.get("cache_creation") or {}
    if "ephemeral_5m_input_tokens" in created or "ephemeral_1h_input_tokens" in created:
        write_5m = created.get("ephemeral_5m_input_tokens") or 0
        write_1h = created.get("ephemeral_1h_input_tokens") or 0
    else:
        write_5m = usage.get("cache_creation_input_tokens") or 0
        write_1h = 0
    return TokenCounts(
        input=usage.get("input_tokens") or 0,
        output=usage.get("output_tokens") or 0,
        cache_read=usage.get("cache_read_input_tokens") or 0,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
    )


def parse_calls(lines: Iterable[str]) -> tuple[list[UsageCall], int]:
    """The model calls in transcript ``lines`` (unpriced), and how many lines
    that mention usage could not be read."""
    calls: list[UsageCall] = []
    unreadable = 0
    for line in lines:
        if '"usage"' not in line:
            continue
        try:
            row = json.loads(line)
            message = row.get("message") or {}
            if row.get("type") != "assistant" or not message.get("usage"):
                continue
            model = message.get("model") or ""
            # Claude Code writes placeholder rows (model "<synthetic>").
            if not model or model.startswith("<") or not message.get("id"):
                continue
            calls.append(
                UsageCall(
                    message_id=message["id"],
                    session_id=row["sessionId"],
                    agent_id=row.get("agentId"),
                    model=model,
                    at=datetime.fromisoformat(row["timestamp"]),
                    cwd=row.get("cwd"),
                    tokens=tokens_of(message["usage"]),
                    usage=message["usage"],
                    cost_usd=None,
                )
            )
        except json.JSONDecodeError, KeyError, ValueError:
            unreadable += 1
    return calls, unreadable
