"""Interactive agent data providers."""

from pathlib import Path

from toolbelt.agent_providers.base import AgentProvider
from toolbelt.agent_providers.claude import ClaudeAgentProvider
from toolbelt.agent_providers.codex import CodexAgentProvider


def providers_for(
    *, claude_projects: Path, state_directory: Path
) -> list[AgentProvider]:
    """The interactive agents whose local usage data Toolbelt can read."""
    return [
        ClaudeAgentProvider(claude_projects),
        CodexAgentProvider(state_directory / "usage" / "codex"),
    ]
