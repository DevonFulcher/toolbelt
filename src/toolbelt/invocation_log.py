"""Persistent, machine-readable log of every toolbelt invocation.

Several agents may run ``tt`` commands at once, across worktrees and repos,
and the console output of each is gone once its terminal scrolls away. This
module appends one JSON object per line to a shared file under the user's
state directory so a later reader (an agent debugging a cross-agent
interaction, or ``tt logs``) can reconstruct what happened.

Every record carries ``ts``, ``event``, ``pid``, ``ppid``, ``inv`` (this
process's invocation id) and ``parent_inv`` (the invocation id of the
toolbelt process that spawned this one, taken from ``TOOLBELT_INVOCATION_ID``
in the environment, or null). ``inv`` is exported to the environment so any
child process, including a nested ``tt``, inherits it as its ``parent_inv``;
filtering on ``inv`` / ``parent_inv`` separates interleaved runs.

Events: ``start`` (argv, cwd, repo), ``exec`` / ``exec_exit`` (subprocess
command, child pid, exit code, duration), ``log`` (anything sent to the
``toolbelt`` logger), ``exit`` (exit code, duration), ``crash`` (traceback).

Concurrency: writers take an ``flock`` on a sidecar lock file around
"rotate if oversized, then append one line", so rotation never races. Lines
are single ``write`` calls in append mode. Logging is strictly best-effort:
the first ``OSError`` (unwritable dir, full disk) disables the writer for
the rest of the process and is otherwise swallowed.

``exec``/``exec_exit``/``exec_failed`` are emitted by ``toolbelt.logged_process``,
the single place toolbelt starts child processes; it reads the log installed
here from ``active``. ``exec_exit`` is emitted when the exit is observed
(``wait``/``poll``/``communicate``), which covers every call site.
"""

import fcntl
import json
import logging
import os
import secrets
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INVOCATION_ID_ENV = "TOOLBELT_INVOCATION_ID"
LOG_DIR_ENV = "TOOLBELT_LOG_DIR"
LOG_MAX_BYTES_ENV = "TOOLBELT_LOG_MAX_BYTES"
LOG_BACKUPS_ENV = "TOOLBELT_LOG_BACKUPS"

LOG_FILENAME = "toolbelt.log.jsonl"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUPS = 3
_MAX_ARG_CHARS = 500


# The log child-process helpers report to; set by ``install``. ``None`` (library
# use, tests) means child processes run unlogged.
active: "InvocationLog | None" = None


def log_dir(env: Mapping[str, str] = os.environ) -> Path:
    """``$TOOLBELT_LOG_DIR``, else ``$XDG_STATE_HOME/toolbelt``, else
    ``~/.local/state/toolbelt``."""
    if env.get(LOG_DIR_ENV):
        return Path(env[LOG_DIR_ENV])
    state_home = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "toolbelt"


def log_path(env: Mapping[str, str] = os.environ) -> Path:
    return log_dir(env) / LOG_FILENAME


class JsonlWriter:
    """Size-bounded, multi-process-safe JSON-lines appender."""

    def __init__(self, path: Path, max_bytes: int, backups: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.disabled = False

    def write(self, record: Mapping[str, Any]) -> None:
        if self.disabled:
            return
        try:
            line = (json.dumps(record, default=str) + "\n").encode()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + ".lock")
            with open(lock_path, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if self._size() >= self.max_bytes:
                    self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
        except OSError:
            self.disabled = True

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _rotate(self) -> None:
        if self.backups <= 0:
            self.path.unlink(missing_ok=True)
            return
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))


def _find_repo_root(cwd: Path) -> str | None:
    """Nearest ancestor with a ``.git`` entry (dir, or file for a worktree)."""
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def truncate_args(args: Sequence[Any]) -> list[str]:
    return [
        a if len(a) <= _MAX_ARG_CHARS else a[:_MAX_ARG_CHARS] + "..."
        for a in (str(x) for x in args)
    ]


class InvocationLog:
    def __init__(
        self,
        writer: JsonlWriter,
        *,
        invocation_id: str,
        parent_invocation_id: str | None,
    ) -> None:
        self.writer = writer
        self.invocation_id = invocation_id
        self.parent_invocation_id = parent_invocation_id
        self.started = time.monotonic()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "InvocationLog":
        writer = JsonlWriter(
            log_path(env),
            max_bytes=int(env.get(LOG_MAX_BYTES_ENV, DEFAULT_MAX_BYTES)),
            backups=int(env.get(LOG_BACKUPS_ENV, DEFAULT_BACKUPS)),
        )
        return cls(
            writer,
            invocation_id=f"{os.getpid()}-{secrets.token_hex(3)}",
            parent_invocation_id=env.get(INVOCATION_ID_ENV) or None,
        )

    def event(self, event: str, **fields: Any) -> None:
        self.writer.write(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "event": event,
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "inv": self.invocation_id,
                "parent_inv": self.parent_invocation_id,
                **fields,
            }
        )


class _ForwardingHandler(logging.Handler):
    def __init__(self, log: InvocationLog) -> None:
        super().__init__()
        self.log = log

    def emit(self, record: logging.LogRecord) -> None:
        self.log.event("log", level=record.levelname, message=record.getMessage())


def install(log: InvocationLog) -> None:
    """Route child-process (via ``toolbelt.logged_process``) and ``toolbelt``
    logger activity into ``log``, and export the invocation id so child processes record it as their parent."""
    os.environ[INVOCATION_ID_ENV] = log.invocation_id
    global active
    active = log
    logging.getLogger("toolbelt").addHandler(_ForwardingHandler(log))


def run_logged(
    entry: Callable[[], None], log: InvocationLog, argv: Sequence[str]
) -> None:
    """Run ``entry`` bracketed by ``start`` and ``exit``/``crash`` records."""
    cwd = Path.cwd()
    log.event(
        "start", argv=truncate_args(argv), cwd=str(cwd), repo=_find_repo_root(cwd)
    )
    try:
        entry()
    except SystemExit as err:
        log.event("exit", code=err.code, duration_ms=_elapsed_ms(log))
        raise
    except BaseException:
        log.event(
            "crash", traceback=traceback.format_exc(), duration_ms=_elapsed_ms(log)
        )
        raise
    log.event("exit", code=0, duration_ms=_elapsed_ms(log))


def _elapsed_ms(log: InvocationLog) -> int:
    return round((time.monotonic() - log.started) * 1000)


def read_records(
    env: Mapping[str, str] = os.environ, invocation: str | None = None
) -> list[str]:
    """Raw log lines, oldest first (newest rotated file, then current),
    optionally only those whose ``inv`` or ``parent_inv`` is ``invocation``."""
    path = log_path(env)
    lines: list[str] = []
    for p in (path.with_name(path.name + ".1"), path):
        try:
            lines.extend(p.read_text().splitlines())
        except FileNotFoundError:
            pass
    if invocation is None:
        return lines
    matched = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if invocation in (rec.get("inv"), rec.get("parent_inv")):
            matched.append(line)
    return matched


def start() -> InvocationLog:
    log = InvocationLog.from_env()
    install(log)
    return log
