"""Claude Code's append-only session transcript provider."""

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from toolbelt.agent_providers.base import AgentProvider
from toolbelt.git.stack.store import UsageCall
from toolbelt.token_spend.pricing import price_usage
from toolbelt.token_spend.transcripts import parse_calls, transcript_files


class ClaudeAgentProvider(AgentProvider):
    def __init__(self, projects: Path) -> None:
        self._projects = projects

    @property
    def name(self) -> Literal["claude"]:
        return "claude"

    def usage_files(self) -> list[Path]:
        return transcript_files(self._projects)

    def parse_usage(self, lines: Iterable[str]) -> tuple[list[UsageCall], int]:
        return parse_calls(lines)

    def price(self, call: UsageCall) -> float | None:
        return price_usage(call.model, call.usage)
