"""Write Codex hook observations in Toolbelt's local data formats."""

# Keep type hints compatible with the Python 3.9 interpreter Codex may launch.
# ruff: noqa: FA100

import fcntl
import json
import os
import re
import shlex
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ACTIVE_TTL_SECONDS = 1800
IDLE_TTL_SECONDS = 1800
VERSION = 1
DETAIL_LIMIT = 500
TOOL_ID_PATTERN = re.compile(r"[^A-Za-z0-9._-]")
SAFE_SUBCOMMANDS = {
    "git": frozenset(
        {
            "add",
            "branch",
            "checkout",
            "cherry-pick",
            "commit",
            "diff",
            "fetch",
            "log",
            "merge",
            "pull",
            "push",
            "rebase",
            "restore",
            "rev-parse",
            "show",
            "status",
            "switch",
            "worktree",
        }
    ),
    "gh": frozenset({"pr", "issue", "run", "repo", "workflow"}),
    "uv": frozenset({"run", "sync", "add", "lock", "tool"}),
    "pnpm": frozenset({"test", "build", "lint", "check", "install"}),
    "npm": frozenset({"test", "run", "install", "build"}),
    "yarn": frozenset({"test", "run", "install", "build"}),
    "docker": frozenset({"build", "compose", "run", "ps", "logs"}),
    "pytest": frozenset(),
    "ruff": frozenset(),
    "mypy": frozenset(),
    "python": frozenset(),
    "python3": frozenset(),
    "node": frozenset(),
    "rg": frozenset(),
    "grep": frozenset(),
    "cat": frozenset(),
    "ls": frozenset(),
    "pwd": frozenset(),
    "curl": frozenset(),
    "jq": frozenset(),
    "toolbelt": frozenset({"git", "ui", "task", "spend"}),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    return datetime.fromisoformat(value)


def _root() -> Path:
    return Path(os.environ.get("TOOLBELT_HOME", Path.home() / ".toolbelt"))


def _safe_id(value: str) -> str:
    return TOOL_ID_PATTERN.sub("_", value)


def _holder(event: dict[str, Any]) -> tuple[str, str, Optional[str]]:
    session_id = str(event["session_id"])
    raw_agent_id = event.get("agent_id")
    agent_id = str(raw_agent_id) if raw_agent_id else None
    suffix = f".{_safe_id(agent_id)}" if agent_id is not None else ""
    return f"{_safe_id(session_id)}{suffix}", session_id, agent_id


def _read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback
    return value if isinstance(value, dict) else fallback


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _write_state(path: Path, value: dict[str, Any]) -> None:
    _atomic_json(path, value)


def _detail(tool_name: str, tool_input: object) -> str:
    """Keep only command names or paths; never persist prompts or tool output."""
    if not isinstance(tool_input, dict):
        return tool_name[:DETAIL_LIMIT]
    command = tool_input.get("command")
    if isinstance(command, str):
        try:
            words = shlex.split(command)
        except ValueError:
            words = []
        if words:
            # Arguments can contain credentials or user data. Keep an
            # allowlisted command and subcommand for activity categorization.
            executable = Path(words[0]).name
            safe_words = [executable if executable in SAFE_SUBCOMMANDS else "command"]
            if len(words) > 1 and words[1] in SAFE_SUBCOMMANDS.get(
                executable, frozenset()
            ):
                safe_words.append(words[1])
            return " ".join(safe_words)[:DETAIL_LIMIT]
        return tool_name[:DETAIL_LIMIT]
    for key in ("file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value[:DETAIL_LIMIT]
    return tool_name[:DETAIL_LIMIT]


def _worktree(cwd: object) -> Optional[str]:
    projects = os.environ.get("GIT_PROJECTS_WORKDIR")
    if not isinstance(cwd, str) or not projects:
        return None
    try:
        candidate = Path(cwd).resolve()
        root = (Path(projects).resolve() / "wt").resolve()
        relative = candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    if len(relative.parts) < 2:
        return None
    return str(root / relative.parts[0] / relative.parts[1])


def _revocations(root: Path, holder: str) -> list[dict[str, Any]]:
    value = _read_json(root / "claims" / "revoked" / f"{holder}.json", {})
    revoked = value.get("revoked", [])
    if not isinstance(revoked, list):
        return []
    return [item for item in revoked if isinstance(item, dict)]


def _claim_survives(claim: dict[str, Any], revocations: list[dict[str, Any]]) -> bool:
    for revoked in revocations:
        same_target = claim.get("kind") == revoked.get("kind") and claim.get(
            "path", claim.get("ref")
        ) == revoked.get("path", revoked.get("ref"))
        if not same_target:
            continue
        try:
            touched_at = _parse_time(claim["touched_at"])
            revoked_at = _parse_time(revoked["at"])
            if touched_at <= revoked_at:
                return False
        except (KeyError, TypeError, ValueError):
            continue
    return True


def _claims_for_event(
    event: dict[str, Any], existing: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    claims = list(existing)
    worktree = _worktree(event.get("cwd"))
    if worktree is not None:
        claims = [
            claim
            for claim in claims
            if not (claim.get("kind") == "worktree" and claim.get("path") == worktree)
        ]
        claims.append({"kind": "worktree", "path": worktree, "touched_at": _iso(now)})
    return claims


def _claim_file(
    root: Path,
    holder: str,
    session_id: str,
    agent_id: Optional[str],
    event: dict[str, Any],
    state: str,
    claims: list[dict[str, Any]],
    now: datetime,
) -> None:
    revoked = _revocations(root, holder)
    remaining = [claim for claim in claims if _claim_survives(claim, revoked)]
    if state == "ended":
        ttl = 0
    elif state == "working":
        ttl = ACTIVE_TTL_SECONDS
    else:
        ttl = IDLE_TTL_SECONDS
    document = {
        "version": VERSION,
        "holder": holder,
        "kind": "agent",
        "session_id": session_id,
        "agent_id": agent_id,
        "link": f"codex://threads/{session_id}",
        "state": "idle" if state == "ended" else state,
        "started_at": event.get("started_at", _iso(now)),
        "updated_at": _iso(now),
        "ttl_seconds": ttl,
        "claims": [] if state == "ended" else remaining,
    }
    activity = event.get("activity")
    if isinstance(activity, dict):
        document["activity"] = activity
    _atomic_json(root / "claims" / f"{holder}.json", document)


def _activity_chunk(
    root: Path,
    holder: str,
    session_id: str,
    agent_id: Optional[str],
    turn_id: str,
    seq: int,
    *,
    started_at: str,
    calls: list[dict[str, Any]],
    final: bool,
    ended_at: Optional[datetime] = None,
    reason: Optional[str] = None,
    totals: Optional[dict[str, int]] = None,
) -> None:
    document = {
        "version": VERSION,
        "holder": holder,
        "session_id": session_id,
        "agent_id": agent_id,
        "turn_id": _safe_id(turn_id),
        "seq": seq,
        "final": final,
        "started_at": started_at,
        "ended_at": _iso(ended_at) if ended_at else None,
        "reason": reason,
        "totals": totals,
        "calls": calls,
    }
    path = root / "activity" / f"{holder}.{_safe_id(turn_id)}.{seq}.json"
    _atomic_json(path, document)


class CodexHook:
    """Handle one Codex hook invocation and update local normalized records."""

    def __init__(self, root: Path, state_dir: Path) -> None:
        self.root = root
        self.state_dir = state_dir

    def handle(self, event: dict[str, Any], *, now: Optional[datetime] = None) -> None:
        event_name = event.get("hook_event_name")
        if not isinstance(event_name, str) or not isinstance(
            event.get("session_id"), str
        ):
            return
        now = now or _now()
        holder, session_id, agent_id = _holder(event)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_dir / f"{holder}.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state_path = self.state_dir / f"{holder}.json"
            state = _read_json(
                state_path,
                {
                    "started_at": _iso(now),
                    "claims": [],
                    "turns": {},
                    "pending": {},
                },
            )
            self._handle_locked(
                event_name, event, state, holder, session_id, agent_id, now
            )
            _write_state(state_path, state)

    def _handle_locked(
        self,
        event_name: str,
        event: dict[str, Any],
        state: dict[str, Any],
        holder: str,
        session_id: str,
        agent_id: Optional[str],
        now: datetime,
    ) -> None:
        if event_name == "UserPromptSubmit":
            state["claims"] = []
            turn_id = event.get("turn_id")
            if isinstance(turn_id, str):
                safe_turn_id = _safe_id(turn_id)
                state.setdefault("turns", {}).setdefault(
                    safe_turn_id,
                    {"started_at": _iso(now), "calls": [], "seq": 0},
                )
        if event_name in {
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PermissionRequest",
            "PreModelRequest",
            "PostModelRequest",
            "SubagentStart",
        }:
            if event_name == "PreToolUse":
                state["claims"] = _claims_for_event(event, state.get("claims", []), now)
            self._write_claim(
                event, state, holder, session_id, agent_id, "working", now
            )
        if event_name == "PreToolUse":
            self._tool_start(event, state, now)
            self._write_claim(
                event, state, holder, session_id, agent_id, "working", now
            )
        elif event_name == "PostToolUse":
            self._tool_end(event, state, holder, session_id, agent_id, now)
        elif event_name == "PostModelRequest":
            self._usage(event, session_id, agent_id, now)
        elif event_name == "PermissionRequest":
            self._write_claim(
                event, state, holder, session_id, agent_id, "blocked", now
            )
        elif event_name in {"Stop", "Interrupt"}:
            self._finish_turn(event, state, holder, session_id, agent_id, now)
            self._write_claim(event, state, holder, session_id, agent_id, "idle", now)
        elif event_name in {"SessionEnd", "SubagentStop"}:
            if event_name == "SessionEnd":
                self._finish_all_turns(state, holder, session_id, agent_id, now)
            else:
                self._finish_turn(event, state, holder, session_id, agent_id, now)
            self._write_claim(event, state, holder, session_id, agent_id, "ended", now)

    def _write_claim(
        self,
        event: dict[str, Any],
        state: dict[str, Any],
        holder: str,
        session_id: str,
        agent_id: Optional[str],
        status: str,
        now: datetime,
    ) -> None:
        event["started_at"] = state["started_at"]
        event["activity"] = state.get("activity")
        _claim_file(
            self.root,
            holder,
            session_id,
            agent_id,
            event,
            status,
            state.get("claims", []),
            now,
        )

    def _tool_start(
        self, event: dict[str, Any], state: dict[str, Any], now: datetime
    ) -> None:
        tool_use_id = event.get("tool_use_id")
        tool_name = event.get("tool_name")
        if not isinstance(tool_use_id, str) or not isinstance(tool_name, str):
            return
        detail = _detail(tool_name, event.get("tool_input"))
        state.setdefault("pending", {})[tool_use_id] = {
            "tool": tool_name,
            "detail": detail,
            "started_at": _iso(now),
            "worktree": _worktree(event.get("cwd")),
            "turn_id": str(event.get("turn_id") or "turn"),
            "delegated": tool_name in {"spawn_agent", "Agent"},
            "background": False,
        }
        state["activity"] = {
            "tool": tool_name,
            "detail": detail[:200],
            "since": _iso(now),
            "open": True,
        }
        turn_id = _safe_id(str(event.get("turn_id") or "turn"))
        turn = state.setdefault("turns", {}).setdefault(
            turn_id, {"started_at": _iso(now), "calls": [], "seq": 0}
        )
        turn.setdefault("started_at", _iso(now))

    def _tool_end(
        self,
        event: dict[str, Any],
        state: dict[str, Any],
        holder: str,
        session_id: str,
        agent_id: Optional[str],
        now: datetime,
    ) -> None:
        tool_use_id = event.get("tool_use_id")
        if isinstance(tool_use_id, str):
            pending = state.setdefault("pending", {}).pop(tool_use_id, None)
        else:
            pending = None
        if not isinstance(pending, dict):
            return
        start = _parse_time(pending["started_at"])
        duration_ms = max(0, int((now - start).total_seconds() * 1000))
        response = event.get("tool_response")
        exit_code = response.get("exit_code") if isinstance(response, dict) else None
        ok = (
            exit_code == 0
            if isinstance(exit_code, int) and not isinstance(exit_code, bool)
            else True
        )
        call = {
            "tool": pending["tool"],
            "detail": pending["detail"],
            "started_at": pending["started_at"],
            "duration_ms": duration_ms,
            "blocked_ms": 0,
            "ok": ok,
            "unended": False,
            "background": pending["background"],
            "delegated": pending["delegated"],
            "worktree": pending["worktree"],
        }
        state["activity"] = {
            "tool": call["tool"],
            "detail": call["detail"][:200],
            "since": _iso(now),
            "open": False,
        }
        turn_id = _safe_id(pending["turn_id"])
        turn = state.setdefault("turns", {}).setdefault(
            turn_id,
            {"started_at": pending["started_at"], "calls": [], "seq": 0},
        )
        turn["calls"].append(call)
        turn["seq"] += 1
        _activity_chunk(
            self.root,
            holder,
            session_id,
            agent_id,
            turn_id,
            turn["seq"],
            started_at=turn["started_at"],
            calls=[call],
            final=False,
        )
        event["activity"] = state["activity"]
        self._write_claim(event, state, holder, session_id, agent_id, "working", now)

    def _usage(
        self,
        event: dict[str, Any],
        session_id: str,
        agent_id: Optional[str],
        now: datetime,
    ) -> None:
        usage = event.get("token_usage")
        model = event.get("model")
        if not isinstance(usage, dict) or not isinstance(model, str):
            return
        directory = self.root / "usage" / "codex"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_safe_id(session_id)}.jsonl"
        row = {
            "version": VERSION,
            "event_id": str(uuid.uuid4()),
            "session_id": session_id,
            "agent_id": agent_id,
            "turn_id": str(event.get("turn_id") or ""),
            "model": model,
            "at": _iso(now),
            "cwd": event.get("cwd") if isinstance(event.get("cwd"), str) else None,
            "token_usage": usage,
        }
        with path.open("a") as output:
            fcntl.flock(output, fcntl.LOCK_EX)
            output.write(json.dumps(row, separators=(",", ":")) + "\n")

    def _finish_all_turns(
        self,
        state: dict[str, Any],
        holder: str,
        session_id: str,
        agent_id: Optional[str],
        now: datetime,
    ) -> None:
        for turn_id in list(state.setdefault("turns", {})):
            self._finish_turn(
                {"turn_id": turn_id, "hook_event_name": "SessionEnd"},
                state,
                holder,
                session_id,
                agent_id,
                now,
            )

    def _finish_turn(
        self,
        event: dict[str, Any],
        state: dict[str, Any],
        holder: str,
        session_id: str,
        agent_id: Optional[str],
        now: datetime,
    ) -> None:
        turn_id = event.get("turn_id")
        if not isinstance(turn_id, str):
            return
        safe_turn_id = _safe_id(turn_id)
        turn = state.setdefault("turns", {}).pop(safe_turn_id, None)
        if not isinstance(turn, dict):
            return
        pending = state.setdefault("pending", {})
        remaining: dict[str, Any] = {}
        unended_calls: list[dict[str, Any]] = []
        for tool_use_id, call in pending.items():
            if _safe_id(str(call.get("turn_id", ""))) != safe_turn_id:
                remaining[tool_use_id] = call
                continue
            start = _parse_time(call["started_at"])
            unended_call = {
                "tool": call["tool"],
                "detail": call["detail"],
                "started_at": call["started_at"],
                "duration_ms": max(0, int((now - start).total_seconds() * 1000)),
                "blocked_ms": 0,
                "ok": None,
                "unended": True,
                "background": call["background"],
                "delegated": call["delegated"],
                "worktree": call["worktree"],
            }
            turn["calls"].append(unended_call)
            unended_calls.append(unended_call)
        state["pending"] = remaining
        started = _parse_time(turn["started_at"])
        wall_ms = max(0, int((now - started).total_seconds() * 1000))
        call_ms = self._interval_union(turn["calls"])
        delegated_calls = [call for call in turn["calls"] if call["delegated"]]
        delegated_ms = self._interval_union(delegated_calls)
        tool_ms = min(wall_ms, max(0, call_ms - delegated_ms))
        delegated_ms = min(wall_ms - tool_ms, delegated_ms)
        _activity_chunk(
            self.root,
            holder,
            session_id,
            agent_id,
            safe_turn_id,
            int(turn.get("seq", 0)) + 1,
            started_at=turn["started_at"],
            calls=unended_calls,
            final=True,
            ended_at=now,
            reason=str(event.get("hook_event_name", "turn_complete")),
            totals={
                "wall_ms": wall_ms,
                "tool_ms": tool_ms,
                "delegated_ms": delegated_ms,
                "blocked_ms": 0,
                "model_ms": max(0, wall_ms - tool_ms - delegated_ms),
            },
        )

    @staticmethod
    def _interval_union(calls: list[dict[str, Any]]) -> int:
        intervals = []
        for call in calls:
            start = _parse_time(call["started_at"])
            end = start + timedelta(milliseconds=call["duration_ms"])
            intervals.append((start, end))
        if not intervals:
            return 0
        intervals.sort()
        first, last = intervals[0]
        total = 0
        for start, end in intervals[1:]:
            if start <= last:
                last = max(last, end)
            else:
                total += int((last - first).total_seconds() * 1000)
                first, last = start, end
        total += int((last - first).total_seconds() * 1000)
        return total


def prompt_context(session_id: str) -> dict[str, Any]:
    """Return the session context for session-aware MCP calls."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"Toolbelt session context: session_id={session_id}; "
                f"session_link=codex://threads/{session_id}. If calling "
                "Toolbelt Decisions, pass this session_id explicitly."
            ),
        }
    }


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(event, dict):
        return 0
    root = _root()
    state_dir = Path(os.environ.get("PLUGIN_DATA", root / "codex-plugin")) / "state"
    CodexHook(root, state_dir).handle(event)
    session_id = event.get("session_id")
    if event.get("hook_event_name") == "UserPromptSubmit" and isinstance(
        session_id, str
    ):
        sys.stdout.write(json.dumps(prompt_context(session_id)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
