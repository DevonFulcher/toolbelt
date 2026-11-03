"""Codex hook usage events, normalized without reading its transcript format."""

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Literal

from toolbelt.agent_providers.base import AgentProvider
from toolbelt.git.stack.store import TokenCounts, UsageCall
from toolbelt.token_spend.pricing import price_codex_usage


class CodexAgentProvider(AgentProvider):
    def __init__(self, usage_directory: Path) -> None:
        self._usage_directory = usage_directory

    @property
    def name(self) -> Literal["codex"]:
        return "codex"

    def usage_files(self) -> list[Path]:
        if not self._usage_directory.is_dir():
            return []
        return sorted(self._usage_directory.glob("*.jsonl"))

    def parse_usage(self, lines: Iterable[str]) -> tuple[list[UsageCall], int]:
        calls: list[UsageCall] = []
        unreadable = 0
        for line in lines:
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("version") != 1:
                    raise ValueError("unsupported event version")
                usage = event["token_usage"]
                if not isinstance(usage, dict):
                    raise TypeError("token_usage is not an object")
                calls.append(
                    UsageCall(
                        message_id=f"codex:{_required_text(event, 'event_id')}",
                        session_id=_required_text(event, "session_id"),
                        agent_id=_optional_text(event.get("agent_id")),
                        model=_required_text(event, "model"),
                        at=datetime.fromisoformat(_required_text(event, "at")),
                        cwd=_optional_text(event.get("cwd")),
                        tokens=codex_token_counts(usage),
                        usage=usage,
                        cost_usd=None,
                        provider=self.name,
                    )
                )
            except KeyError, TypeError, ValueError:
                unreadable += 1
        return calls, unreadable

    def price(self, call: UsageCall) -> float | None:
        return price_codex_usage(call.model, call.tokens)


def codex_token_counts(usage: dict) -> TokenCounts:
    """Map Codex's OpenAI-style usage totals into Toolbelt's token categories."""
    input_value = usage.get("input_tokens")
    if input_value is None:
        input_value = usage.get("prompt_tokens")
    output_value = usage.get("output_tokens")
    if output_value is None:
        output_value = usage.get("completion_tokens")
    input_tokens = _count(input_value)
    cached = usage.get("cached_input_tokens")
    if cached is None:
        details = usage.get("input_tokens_details")
        if not isinstance(details, dict):
            details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
    cache_read = _count(cached)
    return TokenCounts(
        input=max(0, input_tokens - cache_read),
        output=_count(output_value),
        cache_read=cache_read,
    )


def _required_text(event: dict, key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{key} is missing")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return 0
    return int(value)
