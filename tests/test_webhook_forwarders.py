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
        self.error: str | None = None

    def last_error(self) -> str | None:
        return self.error

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

        await s.forwarders.reconcile()

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


class TestProblems:
    """An administered repo whose webhook is not working says why."""

    def seen(self, s: Setup) -> list[str | None]:
        changes: list[str | None] = []
        s.forwarders.on_change = lambda: changes.append(
            p.state if (p := s.forwarders.problem("o/mine")) else None
        )
        return changes

    def test_a_repo_whose_webhook_is_taken_is_blocked(self):
        async def scenario():
            s = Setup({"o/mine"}, admin={"o/mine"})
            changes = self.seen(s)

            async def sleep(seconds: float) -> None:
                await s.release_sleep.wait()

            s.forwarders._sleep = sleep
            await s.forwarders.reconcile()
            await settle()
            process = s.processes[0][2]
            process.error = (
                "error creating webhook: HTTP 422: Validation Failed "
                "(https://api.github.com/repos/o/mine/hooks): "
                "Hook already exists on this repository"
            )
            process.exit(1)
            await settle()

            problem = s.forwarders.problem("o/mine")
            assert problem is not None and problem.state == "blocked"
            assert "one forwarded webhook per repo" in problem.text
            assert changes == ["blocked"]
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_any_other_exit_is_a_failure_with_the_extensions_own_message(self):
        async def scenario():
            s = Setup({"o/mine"}, admin={"o/mine"})

            async def sleep(seconds: float) -> None:
                await s.release_sleep.wait()

            s.forwarders._sleep = sleep
            await s.forwarders.reconcile()
            await settle()
            _, _, process = s.processes[0]
            process.error = "unable to connect to webhooks server, forwarding stopped"
            process.exit(1)
            await settle()

            problem = s.forwarders.problem("o/mine")
            assert problem is not None and problem.state == "failed"
            assert problem.text == (
                "Forwarding stopped: unable to connect to webhooks server, forwarding stopped"
            )
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_an_exit_without_a_message_still_says_something(self):
        async def scenario():
            s = Setup({"o/mine"}, admin={"o/mine"})

            async def sleep(seconds: float) -> None:
                await s.release_sleep.wait()

            s.forwarders._sleep = sleep
            await s.forwarders.reconcile()
            await settle()
            s.processes[0][2].exit(3)
            await settle()

            problem = s.forwarders.problem("o/mine")
            assert problem is not None
            assert problem.text == "The forwarder exited (code 3) without saying why."
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_the_problem_clears_once_the_webhook_is_live(self):
        async def scenario():
            s = Setup({"o/mine"}, admin={"o/mine"})
            changes = self.seen(s)
            state = {"retry": False}

            async def sleep(seconds: float) -> None:
                if seconds == 5 and not state["retry"]:
                    await asyncio.sleep(3600)  # not up long enough yet
                if seconds == 5:
                    return  # the second process stays up
                await s.release_sleep.wait()

            s.forwarders._sleep = sleep
            await s.forwarders.reconcile()
            await settle()
            first = s.processes[0][2]
            first.error = "Hook already exists on this repository"
            first.exit(1)
            await settle()
            assert s.forwarders.problem("o/mine") is not None

            state["retry"] = True
            s.release_sleep.set()
            await settle()

            assert s.forwarders.is_live("o/mine")
            assert s.forwarders.problem("o/mine") is None
            assert changes == ["blocked", None]
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_a_missing_extension_is_a_problem_for_the_repos_the_user_administers(self):
        async def scenario():
            s = Setup({"o/mine", "o/theirs"}, admin={"o/mine"}, installed=False)

            await s.forwarders.reconcile()

            problem = s.forwarders.problem("o/mine")
            assert problem is not None and problem.state == "not_installed"
            assert "gh extension install cli/gh-webhook" in problem.text
            assert s.forwarders.problem("o/theirs") is None  # not administered

            s._installed = True  # installed meanwhile
            await s.forwarders.reconcile()
            await settle()

            assert s.forwarders.problem("o/mine") is None
            assert [slug for slug, _, _ in s.processes] == ["o/mine"]
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_a_repo_no_longer_tracked_has_no_problem(self):
        async def scenario():
            s = Setup({"o/mine"}, admin={"o/mine"}, installed=False)
            await s.forwarders.reconcile()
            assert s.forwarders.problem("o/mine") is not None

            s.slugs = set()
            await s.forwarders.reconcile()

            assert s.forwarders.problem("o/mine") is None

        asyncio.run(scenario())


def test_the_extensions_error_line_is_read_from_its_stderr():
    from toolbelt.ui.webhook_forwarders import _ProcessGroup

    class Process:
        pid = 1
        returncode = 1
        stderr: asyncio.StreamReader

        async def wait(self) -> int:
            return 1

    async def scenario():
        process = Process()
        process.stderr = asyncio.StreamReader()
        process.stderr.feed_data(
            b"Forwarding Webhook events from GitHub...\n"
            b"Error: error creating webhook: Hook already exists on this repository\n"
            b"Usage:\n  forward --events=<types>\n"
        )
        process.stderr.feed_eof()
        group = _ProcessGroup(process)  # type: ignore[arg-type]
        await settle()

        assert group.last_error() == (
            "error creating webhook: Hook already exists on this repository"
        )

    asyncio.run(scenario())
