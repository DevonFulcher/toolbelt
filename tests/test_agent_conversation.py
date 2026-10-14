"""A run's conversation, read from its Claude session transcript."""

import json
from pathlib import Path

from toolbelt.autonomous_agents.conversation import (
    TEXT_LIMIT,
    Entry,
    find_transcript,
    read_conversation,
)


def write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def message(role: str, content, at: str = "2026-10-14T15:00:00Z") -> dict:
    return {
        "type": role,
        "timestamp": at,
        "message": {"role": role, "content": content},
    }


def test_the_prompt_the_text_each_tool_call_and_its_result_are_read_in_order(
    tmp_path: Path,
):
    transcript = write(
        tmp_path / "project" / "s1.jsonl",
        [
            message("user", "Please fix CI."),
            message("assistant", [{"type": "thinking", "thinking": ""}]),
            message("assistant", [{"type": "text", "text": "Looking at the PR."}]),
            message(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "gh pr view 1", "description": "View"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                ],
            ),
            message(
                "user",
                [{"type": "tool_result", "tool_use_id": "t", "content": "OPEN"}],
            ),
            message(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t",
                        "content": [{"type": "text", "text": "boom"}],
                        "is_error": True,
                    }
                ],
            ),
        ],
    )

    assert read_conversation(transcript) == [
        Entry("prompt", "2026-10-14T15:00:00Z", "Please fix CI."),
        Entry("text", "2026-10-14T15:00:00Z", "Looking at the PR."),
        Entry("tool_use", "2026-10-14T15:00:00Z", "View\ngh pr view 1", tool="Bash"),
        Entry("tool_use", "2026-10-14T15:00:00Z", "a.py", tool="Read"),
        Entry("tool_result", "2026-10-14T15:00:00Z", "OPEN"),
        Entry("tool_result", "2026-10-14T15:00:00Z", "boom", is_error=True),
    ]


def test_rows_that_are_not_part_of_the_conversation_are_skipped(tmp_path: Path):
    transcript = write(
        tmp_path / "p" / "s.jsonl",
        [
            {"type": "attachment", "attachment": {}},
            {"type": "cost-state", "totalCostUSD": 1},
            message("assistant", [{"type": "text", "text": "Hi"}]),
        ],
    )

    assert [e.text for e in read_conversation(transcript)] == ["Hi"]


def test_long_output_is_cut_and_says_how_much(tmp_path: Path):
    transcript = write(
        tmp_path / "p" / "s.jsonl",
        [
            message(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t",
                        "content": "x" * (TEXT_LIMIT + 5),
                    }
                ],
            )
        ],
    )

    (entry,) = read_conversation(transcript)

    assert entry.text == "x" * TEXT_LIMIT + "\n… (5 more characters)"


def test_the_transcript_is_found_by_session_id_in_any_project(tmp_path: Path):
    wanted = write(tmp_path / "-wt-a" / "s1.jsonl", [])
    write(tmp_path / "-wt-b" / "s2.jsonl", [])

    assert find_transcript("s1", tmp_path) == wanted
    assert find_transcript("missing", tmp_path) is None
