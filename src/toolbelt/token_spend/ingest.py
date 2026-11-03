"""Brings interactive agents' model calls into the stack store deterministically:
read what each provider appended since last time, price it, and store it.
"""

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from toolbelt.agent_providers.base import AgentProvider
from toolbelt.agent_providers.claude import ClaudeAgentProvider
from toolbelt.git.stack.store import StackStore


@dataclass(frozen=True)
class IngestResult:
    files_read: int = 0
    new_calls: int = 0
    #: Lines that mentioned usage but could not be read.
    unreadable_lines: int = 0
    #: Calls that got a cost this pass although they had none (the price
    #: table learned their model).
    newly_priced: int = 0
    #: Models with calls that still have no price, and how many.
    unpriced_models: dict[str, int] = field(default_factory=dict)


def _read_new(path: Path, offset: int) -> tuple[str, int]:
    """The complete lines appended to ``path`` after ``offset``, as text, and
    the offset after them. A trailing partial line is left for next time; a
    file shorter than ``offset`` was replaced and is read from the start."""
    size = path.stat().st_size
    if size < offset:
        offset = 0
    if size == offset:
        return "", offset
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n") + 1
    return data[:end].decode("utf-8", errors="replace"), offset + end


def ingest_transcripts(store: StackStore, projects: Path) -> IngestResult:
    """Compatibility entry point for ingesting Claude Code transcripts."""
    return ingest_providers(store, [ClaudeAgentProvider(projects)])


def ingest_providers(
    store: StackStore, providers: Sequence[AgentProvider]
) -> IngestResult:
    """Read and price each configured provider's append-only usage sources."""
    files_read = new_calls = unreadable = 0
    for provider in providers:
        for path in provider.usage_files():
            offset = store.transcript_offset(str(path))
            text, new_offset = _read_new(path, offset)
            if new_offset == offset:
                continue
            calls, bad = provider.parse_usage(text.splitlines())
            priced = [
                dataclasses.replace(call, cost_usd=provider.price(call))
                for call in calls
            ]
            new_calls += store.add_usage_calls(
                priced, transcript=str(path), offset=new_offset
            )
            unreadable += bad
            files_read += 1
    newly_priced = _reprice(store, providers)
    return IngestResult(
        files_read=files_read,
        new_calls=new_calls,
        unreadable_lines=unreadable,
        newly_priced=newly_priced,
        unpriced_models=store.unpriced_models(),
    )


def _reprice(store: StackStore, providers: Sequence[AgentProvider]) -> int:
    """Price the stored calls whose model the price table now knows."""
    newly_priced = 0
    provider_by_name = {provider.name: provider for provider in providers}
    for model in store.unpriced_models():
        costs = {}
        for call in store.unpriced_usage(model):
            provider = provider_by_name.get(call.provider)
            if provider is None:
                continue
            cost = provider.price(call)
            if cost is not None:
                costs[call.message_id] = cost
        store.set_usage_costs(costs)
        newly_priced += len(costs)
    return newly_priced
