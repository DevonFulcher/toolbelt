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

Only what toolbelt itself created is ever cleaned up (``forwarder_ledger.py``).
A webhook is recorded as toolbelt's when a forwarder of its own was running as
it appeared (the repo can hold only one) or exited right after creating it.
When a forwarder dies, or is killed before it can close its websocket (a
reboot, a server killed hard), GitHub leaves the webhook behind and it blocks
the repo; GitHub lists it as active until deliveries to it fail, so "active"
does not mean something is connected. A webhook in the ledger is deleted when no
forwarder of ours is alive for the repo (or when GitHub says it is inactive),
and the forwarder is started again at once; one that is not in the ledger
(somebody else's, or of unknown origin) is left alone and reported as blocking.
The pid of each forwarder is recorded too, and a forwarder that outlived a
killed server is stopped at the next start.

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
from toolbelt.ui.forwarder_ledger import Ledger, MemoryLedger
from toolbelt.ui.github_requests import GithubRequests, RateLimited
from toolbelt.ui.github_webhooks import FORWARDED_EVENTS

DEFAULT_RECONCILE_SECONDS = 60.0
DEFAULT_RETRY_INITIAL_SECONDS = 5.0
DEFAULT_RETRY_SECONDS = 60.0
DEFAULT_READY_SECONDS = 5.0
_STOP_GRACE_SECONDS = 5.0
_STDERR_LINES = 20

INSTALL_COMMAND = "gh extension install cli/gh-webhook"
#: Where GitHub's relay receives the webhooks of every ``cli`` hook.
RELAY_URL = "https://webhook-forwarder.github.com/hook"


@dataclass(frozen=True)
class Problem:
    """Why an administered repo's webhook is not working."""

    #: The repo's one forwarded webhook is taken: ``claimed``: by a forwarder
    #: that toolbelt did not start (it is connected now); ``leftover``: by a
    #: webhook toolbelt did not create, with nothing connected to it;
    #: ``blocked``: by something not known (the repo's webhooks could not be
    #: read). ``failed``: the forwarder stopped or could not start;
    #: ``not_installed``: the ``gh`` extension is missing.
    state: Literal["claimed", "leftover", "blocked", "failed", "not_installed"]
    text: str


class ForwarderProcess(Protocol):
    @property
    def pid(self) -> int: ...

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
        self._error: str | None = None
        self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        stream = self._process.stderr
        assert stream is not None
        async for raw in stream:
            line = raw.decode(errors="replace").rstrip()
            self._lines.append(line)
            # The extension prints the usage after an error, which would push
            # the error out of ``_lines``: keep the first one.
            if self._error is None and line.startswith("Error:"):
                self._error = line.removeprefix("Error:").strip()

    def last_error(self) -> str | None:
        if self._error is not None:
            return self._error
        return next((line for line in reversed(self._lines) if line), None)

    @property
    def pid(self) -> int:
        return self._process.pid

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


@dataclass(frozen=True)
class Hook:
    """A forwarded (``cli``) webhook of a repo. ``active`` is true while a
    forwarder is connected to it."""

    id: int
    active: bool


class HookApi(Protocol):
    async def list(self, slug: str) -> list[Hook]:
        """The forwarded webhooks of ``slug``."""
        ...

    async def delete(self, slug: str, hook_id: int) -> None: ...


class GithubHooks:
    """``HookApi`` over the GitHub client."""

    def __init__(self, requests: GithubRequests) -> None:
        self._requests = requests

    async def list(self, slug: str) -> list[Hook]:
        found = await self._requests.get_json(
            f"repos/{slug}/hooks?per_page=100", caller="webhooks"
        )
        return [
            Hook(id=hook["id"], active=hook["active"])
            for hook in found
            if hook["name"] == "cli" and hook["config"].get("url") == RELAY_URL
        ]

    async def delete(self, slug: str, hook_id: int) -> None:
        await self._requests.delete(f"repos/{slug}/hooks/{hook_id}", caller="webhooks")


async def ps_command(pid: int) -> str | None:
    """The command line of process ``pid``, or ``None`` when there is none."""
    process = await logged_process.create_subprocess_exec(
        "ps",
        "-p",
        str(pid),
        "-o",
        "command=",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    command = stdout.decode().strip()
    return command or None


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
        retry_initial_seconds: float = DEFAULT_RETRY_INITIAL_SECONDS,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        ready_seconds: float = DEFAULT_READY_SECONDS,
        hooks: HookApi | None = None,
        ledger: Ledger | None = None,
        process_command: Callable[[int], Awaitable[str | None]] = ps_command,
        signal_group: Callable[[int, int], None] = os.killpg,
    ) -> None:
        """``slugs`` are the tracked repos (``owner/name``); ``url`` is where
        webhooks are replayed to. ``hooks`` reads and deletes a repo's
        forwarded webhooks (without it nothing is ever deleted) and ``ledger``
        records what toolbelt created."""
        self._hooks = hooks
        self._ledger: Ledger = ledger or MemoryLedger()
        self._process_command = process_command
        self._signal_group = signal_group
        self._slugs = slugs
        self._url = url
        self._installed = installed
        self._is_admin = is_admin
        self._spawn = spawn
        self._sleep = sleep
        self._reconcile_seconds = reconcile_seconds
        self._retry_initial_seconds = retry_initial_seconds
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
            if slug not in self._tasks or self._tasks[slug].done():
                self._tasks[slug] = asyncio.create_task(self._forward(slug))

    async def cleanup_orphans(self) -> None:
        """Stops the forwarders a previous server left running when it was
        killed. Only a recorded pid whose command line is still a forwarder
        for that repo is signalled (the pid may belong to something else
        by now)."""
        for slug, pid in self._ledger.pids().items():
            command = await self._process_command(pid)
            if command is not None and _is_forwarder_of(command, slug):
                logger.warning(
                    "stopping the forwarder of %s left by an earlier run", slug
                )
                with contextlib.suppress(ProcessLookupError):
                    self._signal_group(pid, signal.SIGINT)
                    for _ in range(int(_STOP_GRACE_SECONDS * 10)):
                        if await self._process_command(pid) is None:
                            break
                        await self._sleep(0.1)
                    else:
                        self._signal_group(pid, signal.SIGKILL)
            self._ledger.clear_pid(slug)

    async def run(self) -> None:
        await self.cleanup_orphans()
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
        """Keeps one forwarder process up, restarting it when it exits: after
        ``retry_initial_seconds``, then twice as long each time it fails to come
        up, to at most ``retry_seconds``; a forwarder that was up starts over
        from the short wait (a dropped connection, a laptop waking). Whatever
        goes wrong while supervising it (a network error asking GitHub for the
        repo's webhooks, say) is waited out the same way: the loop never ends
        until the repo is no longer wanted."""
        delay = self._retry_initial_seconds
        while True:
            try:
                if await self._forward_once(slug):
                    delay = self._retry_initial_seconds
                # A webhook of ours that was left inactive is gone now: start
                # again at once instead of waiting out the retry delay.
                if await self._reclaim(slug):
                    continue
            except Exception as failure:  # noqa: BLE001 - the loop must go on
                logger.exception("supervising the forwarder of %s failed", slug)
                self._set_problem(
                    slug,
                    Problem("failed", f"The forwarder could not be started: {failure}"),
                )
            await self._sleep(delay)
            delay = min(delay * 2, self._retry_seconds)

    async def _forward_once(self, slug: str) -> bool:
        """Runs one forwarder process until it exits. Returns whether it stayed
        up long enough to count as live."""
        await self._reclaim(slug)
        before = await self._hook_ids(slug)
        process = await self._spawn(slug, self._url)
        self._ledger.set_pid(slug, process.pid)
        exited = asyncio.ensure_future(process.wait())
        ready = asyncio.ensure_future(self._sleep(self._ready_seconds))
        came_up = False
        try:
            await asyncio.wait({ready, exited}, return_when=asyncio.FIRST_COMPLETED)
            if not exited.done():
                self._live.add(slug)
                self._set_problem(slug, None)
                came_up = True
                await self._record_created(slug, before)
            await exited
            problem = _problem_of(process)
            if problem.state == "blocked":
                problem = await self._who_blocks(slug) or problem
            else:
                # It did not fail because the repo's webhook was taken, so
                # one that appeared meanwhile is its own.
                await self._record_created(slug, before)
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
            self._ledger.clear_pid(slug)
        return came_up

    async def _who_blocks(self, slug: str) -> Problem | None:
        """Says what holds the repo's one webhook when it is not toolbelt's:
        a forwarder connected to it, or one nothing is connected to."""
        if self._hooks is None:
            return None
        try:
            found = await self._hooks.list(slug)
        except RuntimeError:  # rate limited, or GitHub refused: not known
            return None
        outside = [hook for hook in found if hook.id not in self._ledger.hooks(slug)]
        if not outside:
            return None
        if any(hook.active for hook in outside):
            return Problem(
                "claimed",
                "Another forwarder, not started by toolbelt, is connected to this "
                "repo's webhook. GitHub allows one per repo, so toolbelt cannot "
                "forward it until that stops.",
            )
        return Problem(
            "leftover",
            "A forwarded webhook that toolbelt did not create is registered on this "
            "repo with nothing connected to it, so toolbelt cannot forward it. "
            "Delete it in the repo's webhook settings if it is yours.",
        )

    async def _hook_ids(self, slug: str) -> set[int] | None:
        """The ids of the repo's forwarded webhooks (``None``: not known)."""
        if self._hooks is None:
            return None
        try:
            return {hook.id for hook in await self._hooks.list(slug)}
        except RuntimeError:  # rate limited, or GitHub refused: not known
            return None

    async def _record_created(self, slug: str, before: set[int] | None) -> None:
        """Records the webhooks that appeared since ``before`` as toolbelt's:
        the repo holds one, and it was made while a forwarder of ours ran."""
        now = await self._hook_ids(slug)
        if before is None or now is None:
            return
        for hook_id in now - before:
            self._ledger.add_hook(slug, hook_id)

    async def _forwarder_alive(self, slug: str) -> bool:
        """Whether a forwarder recorded for ``slug`` is still running (one that
        another server of this machine started: ours are cleared when they end
        and a killed server's are stopped at the next start)."""
        pid = self._ledger.pids().get(slug)
        if pid is None:
            return False
        command = await self._process_command(pid)
        return command is not None and _is_forwarder_of(command, slug)

    async def _reclaim(self, slug: str) -> bool:
        """Deletes the webhooks of ``slug`` that toolbelt created and nothing is
        connected to: GitHub lists them inactive, or no forwarder of ours is
        alive for the repo (nothing else is ever deleted). Returns whether it
        deleted any."""
        if self._hooks is None:
            return False
        ours = self._ledger.hooks(slug)
        if not ours:
            return False
        try:
            found = await self._hooks.list(slug)
        except RuntimeError:  # rate limited, or GitHub refused: leave it be
            return False
        for gone in ours - {hook.id for hook in found}:
            self._ledger.forget_hook(slug, gone)
        deleted = False
        alive = await self._forwarder_alive(slug)
        for hook in found:
            if hook.id in ours and (not hook.active or not alive):
                try:
                    await self._hooks.delete(slug, hook.id)
                except RateLimited:
                    return deleted
                except RuntimeError as failure:
                    logger.warning(
                        "could not delete the leftover webhook %s of %s: %s",
                        hook.id,
                        slug,
                        failure,
                    )
                    continue
                self._ledger.forget_hook(slug, hook.id)
                logger.warning(
                    "deleted toolbelt's leftover webhook %s of %s", hook.id, slug
                )
                deleted = True
        return deleted


def _is_forwarder_of(command: str, slug: str) -> bool:
    return "webhook" in command and "forward" in command and f"--repo={slug}" in command


def _problem_of(process: ForwarderProcess) -> Problem:
    error = process.last_error()
    # ``gh`` gives "Hook already exists" or, when GitHub does not say why, only
    # the 422 status: either way the repo's one forwarded webhook is taken.
    if error is not None and ("Hook already exists" in error or "HTTP 422" in error):
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
