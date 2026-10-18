"""Gets GitHub's webhooks to the UI for the repos where that is allowed.

Webhooks need a URL GitHub can reach. The GitHub CLI's ``webhook`` extension
gets around that: ``gh webhook forward`` registers a temporary webhook on a
repo (which takes admin rights on it), receives its events over a websocket
and replays them as POSTs to a local URL, and removes the webhook when it
exits. ``WebhookForwarders`` keeps one such process running for each tracked
repo the user administers, for as long as the UI serves. Repos without admin
rights, and every repo while the extension is not installed, are left to
polling.

A repo counts as ``live`` once its process has stayed up for ``ready_seconds``
(it exits at once when it cannot register the webhook). The status refresher
then polls that repo only as a safety net.

Process start and the permission lookup are seams so tests inject fakes.
"""

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable
from typing import Protocol

from toolbelt import logged_process
from toolbelt.logger import logger
from toolbelt.ui.github_requests import RateLimited
from toolbelt.ui.github_webhooks import FORWARDED_EVENTS

DEFAULT_RECONCILE_SECONDS = 60.0
DEFAULT_RETRY_SECONDS = 60.0
DEFAULT_READY_SECONDS = 5.0
_STOP_GRACE_SECONDS = 5.0


class ForwarderProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


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
            stderr=asyncio.subprocess.DEVNULL,
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

    def is_live(self, slug: str) -> bool:
        return slug in self._live

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
        for slug in wanted:
            if self._admin.get(slug) and slug not in self._tasks:
                self._tasks[slug] = asyncio.create_task(self._forward(slug))

    async def run(self) -> None:
        if not await self._installed():
            logger.info(
                "GitHub webhooks are not forwarded: install the extension "
                "with `gh extension install cli/gh-webhook`"
            )
            return
        try:
            while True:
                await self.reconcile()
                await self._sleep(self._reconcile_seconds)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Ends every forwarder (each removes its webhook from its repo)."""
        for slug in list(self._tasks):
            await self._stop(slug)

    async def _stop(self, slug: str) -> None:
        task = self._tasks.pop(slug)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._live.discard(slug)

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
                await exited
                logger.warning(
                    "webhook forwarder for %s exited with %s", slug, process.returncode
                )
            finally:
                ready.cancel()
                exited.cancel()
                self._live.discard(slug)
                await _terminate(process)
            await self._sleep(self._retry_seconds)


async def _terminate(process: ForwarderProcess) -> None:
    """Ends a running forwarder the way Ctrl-C does, so it removes its
    webhook from the repo; kills it if it does not exit."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), _STOP_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
