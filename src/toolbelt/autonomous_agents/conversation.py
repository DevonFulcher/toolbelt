"""What an autonomous run said and did, read from its Claude session.

A run uses a session id chosen up front (``agent_runs.session_id``), so its
transcript is ``<projects>/<project>/<session id>.jsonl`` from the first
message on: the prompt, the agent's text, each tool it called and what came
back. Reading it while the run works shows it live.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

#: Longest tool input or output kept per entry.
TEXT_LIMIT = 3000

type EntryKind = Literal["prompt", "text", "tool_use", "tool_result"]


@dataclass(frozen=True)
class Entry:
    kind: EntryKind
    at: str
    text: str
    #: The tool called (``tool_use``).
    tool: str | None = None
    #: The tool reported an error (``tool_result``).
    is_error: bool = False


def find_transcript(session_id: str, projects: Path) -> Path | None:
    return next(iter(projects.glob(f"*/{session_id}.jsonl")), None)


def _clip(text: str) -> str:
    if len(text) <= TEXT_LIMIT:
        return text
    return f"{text[:TEXT_LIMIT]}\n… ({len(text) - TEXT_LIMIT} more characters)"


def _tool_input(name: str, tool_input: dict[str, Any]) -> str:
    """The part of a tool call worth reading: the command, or the file."""
    if name == "Bash":
        command = str(tool_input.get("command", ""))
        description = tool_input.get("description")
        return f"{description}\n{command}" if description else command
    for key in ("file_path", "pattern", "path"):
        if key in tool_input:
            return str(tool_input[key])
    return json.dumps(tool_input)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "") for block in content if isinstance(block, dict)
    )


def _entries(row: dict[str, Any]) -> list[Entry]:
    kind = row.get("type")
    if kind not in ("user", "assistant"):
        return []
    at = row.get("timestamp", "")
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return [Entry("prompt", at, _clip(content))] if kind == "user" else []
    entries = []
    for block in content or []:
        match block.get("type"):
            case "text" if block.get("text"):
                entries.append(Entry("text", at, _clip(block["text"])))
            case "tool_use":
                name = block["name"]
                text = _clip(_tool_input(name, block.get("input") or {}))
                entries.append(Entry("tool_use", at, text, tool=name))
            case "tool_result":
                text = _clip(_result_text(block.get("content")))
                entries.append(
                    Entry("tool_result", at, text, is_error=bool(block.get("is_error")))
                )
    return entries


def read_conversation(transcript: Path) -> list[Entry]:
    entries: list[Entry] = []
    for line in transcript.read_text().splitlines():
        entries.extend(_entries(json.loads(line)))
    return entries
