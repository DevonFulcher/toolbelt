"""The `gh webhook forward` processes: one per repo the user administers, up
while the UI serves, restarted when they exit, and the repo reported live once
its process has stayed up."""

import asyncio
import signal

from toolbelt.ui.webhook_forwarders import WebhookForwarders


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self.signals: list[int] = []

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def exit(self, code: int = 1) -> None:
        self.returncode = code
        self._exited.set()

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.exit(0)

    def kill(self) -> None:
        self.exit(-9)


class Setup:
    def __init__(self, slugs: set[str], *, admin: set[str], installed: bool = True):
        self.slugs = slugs
        self.admin = admin
        self.processes: list[tuple[str, str, FakeProcess]] = []
        self.sleeps: list[float] = []
        self.release_sleep = asyncio.Event()
        self.forwarders = WebhookForwarders(
            slugs=self.tracked,
            url="http://127.0.0.1:1/api/github-webhook",
            installed=self.is_installed,
            is_admin=self.is_admin,
            spawn=self.spawn,
            sleep=self.sleep,
            ready_seconds=5,
            retry_seconds=60,
        )
        self._installed = installed

    async def tracked(self) -> set[str]:
        return self.slugs

    async def is_installed(self) -> bool:
        return self._installed

    async def is_admin(self, slug: str) -> bool:
        return slug in self.admin

    async def spawn(self, slug: str, url: str) -> FakeProcess:
        process = FakeProcess()
        self.processes.append((slug, url, process))
        return process

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds == 5:  # the readiness wait: the process has stayed up
            return
        await self.release_sleep.wait()


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def test_a_forwarder_runs_for_each_repo_the_user_administers_only():
    async def scenario():
        s = Setup({"o/mine", "o/theirs"}, admin={"o/mine"})

        await s.forwarders.reconcile()
        await settle()

        assert [(slug, url) for slug, url, _ in s.processes] == [
            ("o/mine", "http://127.0.0.1:1/api/github-webhook")
        ]
        assert s.forwarders.is_live("o/mine")
        assert not s.forwarders.is_live("o/theirs")
        await s.forwarders.stop()

    asyncio.run(scenario())


def test_a_repo_is_not_live_until_its_process_has_stayed_up():
    async def scenario():
        s = Setup({"o/mine"}, admin={"o/mine"})
        gate = asyncio.Event()

        async def sleep(seconds: float) -> None:
            if seconds == 5:
                await gate.wait()
            else:
                await s.release_sleep.wait()

        s.forwarders._sleep = sleep
        await s.forwarders.reconcile()
        await settle()
        assert not s.forwarders.is_live("o/mine")

        gate.set()
        await settle()

        assert s.forwarders.is_live("o/mine")

    asyncio.run(scenario())


def test_a_process_that_exits_at_once_is_never_live_and_is_restarted_later():
    async def scenario():
        s = Setup({"o/mine"}, admin={"o/mine"})

        async def sleep(seconds: float) -> None:
            s.sleeps.append(seconds)
            await s.release_sleep.wait()

        s.forwarders._sleep = sleep
        await s.forwarders.reconcile()
        await settle()
        _, _, first = s.processes[0]
        first.exit(1)
        await settle()

        assert not s.forwarders.is_live("o/mine")
        assert len(s.processes) == 1  # waiting out the retry delay
        s.release_sleep.set()
        await settle()
        assert len(s.processes) == 2

    asyncio.run(scenario())


def test_a_repo_that_is_no_longer_tracked_has_its_forwarder_stopped_with_ctrl_c():
    async def scenario():
        s = Setup({"o/mine"}, admin={"o/mine"})
        await s.forwarders.reconcile()
        await settle()
        _, _, process = s.processes[0]

        s.slugs = set()
        await s.forwarders.reconcile()

        assert process.signals == [signal.SIGINT]
        assert not s.forwarders.is_live("o/mine")

    asyncio.run(scenario())


def test_nothing_runs_when_the_extension_is_not_installed():
    async def scenario():
        s = Setup({"o/mine"}, admin={"o/mine"}, installed=False)

        await s.forwarders.run()

        assert s.processes == []

    asyncio.run(scenario())


def test_stopping_the_manager_stops_every_forwarder():
    async def scenario():
        s = Setup({"o/mine"}, admin={"o/mine"})
        task = asyncio.create_task(s.forwarders.run())
        await settle()
        _, _, process = s.processes[0]

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert process.signals == [signal.SIGINT]

    asyncio.run(scenario())


def test_a_rate_limit_while_checking_admin_rights_asks_again_at_the_next_reconcile():
    from toolbelt.ui.github_requests import RateLimited

    async def scenario():
        s = Setup({"o/mine"}, admin={"o/mine"})
        limited = [True]

        async def is_admin(slug: str) -> bool:
            if limited[0]:
                raise RateLimited("core", 60)
            return True

        s.forwarders._is_admin = is_admin
        await s.forwarders.reconcile()
        await settle()
        assert s.processes == []

        limited[0] = False
        await s.forwarders.reconcile()
        await settle()

        assert [slug for slug, _, _ in s.processes] == ["o/mine"]
        await s.forwarders.stop()

    asyncio.run(scenario())
