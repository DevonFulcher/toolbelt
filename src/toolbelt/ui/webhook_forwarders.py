"""Gets GitHub's webhooks to the UI for the repos where that is allowed.

Webhooks need a URL GitHub can reach. The GitHub CLI's ``webhook`` extension
gets around that: ``gh webhook forward`` registers a temporary webhook on a
repo (which takes admin rights on it), receives its events over a websocket
and replays them as POSTs to a local URL; GitHub removes the webhook when the
websocket closes. GitHub allows one forwarded webhook per repo, whoever made
it. ``WebhookForwarders`` keeps one such process running for each tracked
repo the user administers, for as long as the UI serves. Repos without admin
rights, and every repo while the extension is not installed, are left to
polling.

A repo counts as ``live`` once its process has stayed up for ``ready_seconds``
(it exits at once when it cannot register the webhook). The status refresher
then polls that repo only as a safety net.

A repo the user administers whose webhook is not live has a ``Problem``
(``problem(slug)``) saying why, from the extension's own error: another
forwarder or a leftover webhook holds the repo, the forwarder stopped, or the
extension is not installed. ``on_change`` is called when the problems change.

Process start and the permission lookup are seams so tests inject fakes.
"""

import asyncio
import contextlib
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from toolbelt import logged_process
from toolbelt.logger import logger
from toolbelt.ui.github_requests import RateLimited
from toolbelt.ui.github_webhooks import FORWARDED_EVENTS

DEFAULT_RECONCILE_SECONDS = 60.0
DEFAULT_RETRY_SECONDS = 60.0
DEFAULT_READY_SECONDS = 5.0
_STOP_GRACE_SECONDS = 5.0
_STDERR_LINES = 20

INSTALL_COMMAND = "gh extension install cli/gh-webhook"


@dataclass(frozen=True)
class Problem:
    """Why an administered repo's webhook is not working."""

    #: ``blocked``: the repo's one forwarded webhook is held by another
    #: forwarder or left behind; ``failed``: the forwarder stopped or could not
    #: start; ``not_installed``: the ``gh`` extension is missing.
    state: Literal["blocked", "failed", "not_installed"]
    text: str


class ForwarderProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...

    def last_error(self) -> str | None:
        """The extension's error message, once it has printed one."""
        ...


#: ``owner/name``, the local URL to replay webhooks to -> the running process.
Spawn = Callable[[str, str], Awaitable[ForwarderProcess]]
#: ``owner/name`` -> whether the user has admin rights on it.
AdminCheck = Callable[[str], Awaitable[bool]]


class _ProcessGroup:
    """A forwarder started as the leader of its own process group. ``gh``
    runs the extension as a child, so ending the forwarder means signalling
    the group, not just ``gh``."""

    def __init__(self, process: logged_process.AsyncProcess) -> None:
        self._process = process
        self._lines: deque[str] = deque(maxlen=_STDERR_LINES)
        self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        stream = self._process.stderr
        assert stream is not None
        async for line in stream:
            self._lines.append(line.decode(errors="replace").rstrip())

    def last_error(self) -> str | None:
        for line in self._lines:
            if line.startswith("Error:"):
                return line.removeprefix("Error:").strip()
        return next((line for line in reversed(self._lines) if line), None)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        return await self._process.wait()

    def send_signal(self, sig: int) -> None:
        os.killpg(self._process.pid, sig)

    def kill(self) -> None:
        os.killpg(self._process.pid, signal.SIGKILL)


async def gh_spawn(slug: str, url: str) -> ForwarderProcess:
    return _ProcessGroup(
        await logged_process.create_subprocess_exec(
            "gh",
            "webhook",
            "forward",
            f"--repo={slug}",
            f"--events={','.join(FORWARDED_EVENTS)}",
            f"--url={url}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )


async def gh_extension_installed() -> bool:
    process = await logged_process.create_subprocess_exec(
        "gh",
        "webhook",
        "--help",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait() == 0


class WebhookForwarders:
    def __init__(
        self,
        *,
        slugs: Callable[[], Awaitable[set[str]]],
        url: str,
        installed: Callable[[], Awaitable[bool]] = gh_extension_installed,
        is_admin: AdminCheck,
        spawn: Spawn = gh_spawn,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        reconcile_seconds: float = DEFAULT_RECONCILE_SECONDS,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        ready_seconds: float = DEFAULT_READY_SECONDS,
    ) -> None:
        """``slugs`` are the tracked repos (``owner/name``); ``url`` is where
        webhooks are replayed to."""
        self._slugs = slugs
        self._url = url
        self._installed = installed
        self._is_admin = is_admin
        self._spawn = spawn
        self._sleep = sleep
        self._reconcile_seconds = reconcile_seconds
        self._retry_seconds = retry_seconds
        self._ready_seconds = ready_seconds
        self._admin: dict[str, bool] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._live: set[str] = set()
        self._problems: dict[str, Problem] = {}
        self._extension = False
        #: Called when ``problem`` changes for some repo.
        self.on_change: Callable[[], None] | None = None

    def is_live(self, slug: str) -> bool:
        return slug in self._live

    def problem(self, slug: str) -> Problem | None:
        """Why the webhook of ``slug`` (a repo the user administers) is not
        working, or ``None`` when it is, or is not wanted."""
        return self._problems.get(slug)

    def _set_problem(self, slug: str, problem: Problem | None) -> None:
        if self._problems.get(slug) == problem:
            return
        if problem is None:
            del self._problems[slug]
        else:
            self._problems[slug] = problem
        if self.on_change is not None:
            self.on_change()

    async def reconcile(self) -> None:
        """Starts a forwarder for each tracked repo the user administers and
        stops those of repos no longer tracked."""
        wanted = await self._slugs()
        for slug in wanted - self._admin.keys():
            try:
                self._admin[slug] = await self._is_admin(slug)
            except RateLimited:
                break  # asked again at the next reconcile
        for slug in set(self._tasks) - wanted:
            await self._stop(slug)
        for slug in set(self._problems) - wanted:
            self._set_problem(slug, None)
        if not self._extension:
            self._extension = await self._installed()
        administered = {slug for slug in wanted if self._admin.get(slug)}
        if not self._extension:
            for slug in administered:
                self._set_problem(
                    slug,
                    Problem(
                        "not_installed",
                        f"The GitHub CLI extension is not installed: {INSTALL_COMMAND}",
                    ),
                )
            return
        for slug in administered:
            if slug not in self._tasks:
                self._tasks[slug] = asyncio.create_task(self._forward(slug))

    async def run(self) -> None:
        try:
            while True:
                await self.reconcile()
                await self._sleep(self._reconcile_seconds)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Ends every forwarder (GitHub removes each one's webhook once its
        websocket closes)."""
        for slug in list(self._tasks):
            await self._stop(slug)

    async def _stop(self, slug: str) -> None:
        task = self._tasks.pop(slug)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._live.discard(slug)
        self._set_problem(slug, None)

    async def _forward(self, slug: str) -> None:
        """Keeps one forwarder process up, restarting it when it exits."""
        while True:
            process = await self._spawn(slug, self._url)
            exited = asyncio.ensure_future(process.wait())
            ready = asyncio.ensure_future(self._sleep(self._ready_seconds))
            try:
                await asyncio.wait({ready, exited}, return_when=asyncio.FIRST_COMPLETED)
                if not exited.done():
                    self._live.add(slug)
                    self._set_problem(slug, None)
                await exited
                problem = _problem_of(process)
                self._set_problem(slug, problem)
                logger.warning(
                    "webhook forwarder for %s exited with %s: %s",
                    slug,
                    process.returncode,
                    problem.text,
                )
            finally:
                ready.cancel()
                exited.cancel()
                self._live.discard(slug)
                await _terminate(process)
            await self._sleep(self._retry_seconds)


def _problem_of(process: ForwarderProcess) -> Problem:
    error = process.last_error()
    if error is not None and "Hook already exists" in error:
        return Problem(
            "blocked",
            "GitHub allows one forwarded webhook per repo, and this one is taken: "
            "another person is forwarding it, or a leftover webhook blocks it.",
        )
    if error is not None:
        return Problem("failed", f"Forwarding stopped: {error}")
    return Problem(
        "failed",
        f"The forwarder exited (code {process.returncode}) without saying why.",
    )


async def _terminate(process: ForwarderProcess) -> None:
    """Ends a running forwarder the way Ctrl-C does, so its websocket closes
    cleanly and GitHub removes its webhook; kills it if it does not exit."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), _STOP_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
