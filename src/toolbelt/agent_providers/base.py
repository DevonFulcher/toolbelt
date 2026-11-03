"""Common interface for ingesting interactive-agent usage."""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

from toolbelt.git.stack.store import AgentProviderName, UsageCall


class AgentProvider(ABC):
    """Reads one agent's native usage source and normalizes it for storage."""

    @property
    @abstractmethod
    def name(self) -> AgentProviderName:
        """Stable name stored with usage calls."""

    @abstractmethod
    def usage_files(self) -> list[Path]:
        """The append-only usage sources currently available."""

    @abstractmethod
    def parse_usage(self, lines: Iterable[str]) -> tuple[list[UsageCall], int]:
        """Parse source lines into calls and return the number of unreadable lines."""

    @abstractmethod
    def price(self, call: UsageCall) -> float | None:
        """Price a normalized call at public list prices, when supported."""
