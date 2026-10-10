"""The one place toolbelt starts child processes.

Every subprocess goes through here so the invocation log
(`toolbelt.invocation_log`) records `exec` / `exec_exit` / `exec_failed` for
it. The functions mirror the `subprocess` / `asyncio` ones of the same name,
so call sites only change their module prefix. When no invocation log is
active (library use, tests) they behave exactly like the stdlib versions.

Direct use of `subprocess.run`, `subprocess.Popen`, `subprocess.check_output`
and `asyncio.create_subprocess_exec` elsewhere is banned by ruff (TID251, see
`pyproject.toml`) so new call sites don't silently skip the log.
"""

import asyncio
import subprocess
import time
from collections.abc import Sequence
from typing import Any

from toolbelt import invocation_log


def _cmd(args: Any) -> list[str]:
    return invocation_log.truncate_args(
        args if isinstance(args, (list, tuple)) else [args]
    )


def _log_exit(child_pid: int, returncode: int, started: float) -> None:
    log = invocation_log.active
    if log is not None:
        log.event(
            "exec_exit",
            child_pid=child_pid,
            returncode=returncode,
            duration_ms=round((time.monotonic() - started) * 1000),
        )


class Popen(subprocess.Popen):  # type: ignore[type-arg]
    """`subprocess.Popen` that logs its start and, once observed, its exit."""

    def __init__(self, args: Any, *a: Any, **kw: Any) -> None:
        self._started = time.monotonic()
        self._exit_logged = False
        log = invocation_log.active
        try:
            super().__init__(args, *a, **kw)
        except OSError as err:
            if log is not None:
                log.event(
                    "exec_failed", cmd=_cmd(args), cwd=kw.get("cwd"), error=repr(err)
                )
            raise
        if log is not None:
            log.event("exec", cmd=_cmd(args), cwd=kw.get("cwd"), child_pid=self.pid)

    def _log_exit_once(self) -> None:
        if self.returncode is not None and not self._exit_logged:
            self._exit_logged = True
            _log_exit(self.pid, self.returncode, self._started)

    def wait(self, timeout: float | None = None) -> int:
        try:
            return super().wait(timeout)
        finally:
            self._log_exit_once()

    def poll(self) -> int | None:
        try:
            return super().poll()
        finally:
            self._log_exit_once()


def run(
    args: Any,
    *,
    input: str | bytes | None = None,
    capture_output: bool = False,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """`subprocess.run` (minus `timeout`), logged."""
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    with Popen(args, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input)
        except BaseException:
            process.kill()
            raise
        returncode = process.poll()
    assert returncode is not None
    if check and returncode:
        raise subprocess.CalledProcessError(returncode, args, stdout, stderr)
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def check_output(args: Any, **kwargs: Any) -> Any:
    """`subprocess.check_output`, logged."""
    return run(args, stdout=subprocess.PIPE, check=True, **kwargs).stdout


class AsyncProcess:
    """An `asyncio` subprocess whose exit is logged when `communicate` or
    `wait` observes it."""

    def __init__(self, process: asyncio.subprocess.Process, started: float) -> None:
        self._process = process
        self._started = started

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        result = await self._process.communicate(input)
        self._log_exit()
        return result

    async def wait(self) -> int:
        code = await self._process.wait()
        self._log_exit()
        return code

    def _log_exit(self) -> None:
        assert self._process.returncode is not None
        _log_exit(self.pid, self._process.returncode, self._started)


async def create_subprocess_exec(
    program: str, *args: str, **kwargs: Any
) -> AsyncProcess:
    """`asyncio.create_subprocess_exec`, logged."""
    log = invocation_log.active
    cmd: Sequence[str] = [program, *args]
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(program, *args, **kwargs)
    except OSError as err:
        if log is not None:
            log.event(
                "exec_failed", cmd=_cmd(cmd), cwd=kwargs.get("cwd"), error=repr(err)
            )
        raise
    if log is not None:
        log.event("exec", cmd=_cmd(cmd), cwd=kwargs.get("cwd"), child_pid=process.pid)
    return AsyncProcess(process, started)
