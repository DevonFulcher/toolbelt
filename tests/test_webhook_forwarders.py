"""The `gh webhook forward` processes: one per repo the user administers, up
while the UI serves, restarted when they exit, and the repo reported live once
its process has stayed up."""

import asyncio
import signal

from toolbelt.ui.forwarder_ledger import MemoryLedger
from toolbelt.ui.webhook_forwarders import Hook, WebhookForwarders


class FakeProcess:
    pids = iter(range(1000, 100000))

    def __init__(self) -> None:
        self.pid = next(FakeProcess.pids)
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
    def __init__(
        self,
        slugs: set[str],
        *,
        admin: set[str],
        installed: bool = True,
        **kwargs,
    ):
        #: Called with each process just spawned (what the extension does on start).
        self.on_spawn = None
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
            retry_initial_seconds=7,
            retry_seconds=60,
            **kwargs,
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
        if self.on_spawn is not None:
            self.on_spawn(slug, process)
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


class FakeHooks:
    """GitHub's forwarded webhooks of each repo."""

    def __init__(self) -> None:
        self.by_slug: dict[str, list[Hook]] = {}
        self.deleted: list[tuple[str, int]] = []
        self._next = 100

    def create(self, slug: str, *, active: bool) -> Hook:
        hook = Hook(id=self._next, active=active)
        self._next += 1
        self.by_slug.setdefault(slug, []).append(hook)
        return hook

    def activate(self, slug: str, hook: Hook, active: bool) -> None:
        self.by_slug[slug] = [
            Hook(h.id, active) if h.id == hook.id else h for h in self.by_slug[slug]
        ]

    async def list(self, slug: str) -> list[Hook]:
        return list(self.by_slug.get(slug, []))

    async def delete(self, slug: str, hook_id: int) -> None:
        self.by_slug[slug] = [h for h in self.by_slug.get(slug, []) if h.id != hook_id]
        self.deleted.append((slug, hook_id))


def owned(admin: str = "o/mine"):
    hooks = FakeHooks()
    ledger = MemoryLedger()
    s = Setup({admin}, admin={admin}, hooks=hooks, ledger=ledger)
    return s, hooks, ledger


class TestOwnedHooks:
    """Only the webhooks toolbelt created are ever cleaned up."""

    def test_the_webhook_of_a_live_forwarder_is_recorded_as_ours(self):
        async def scenario():
            s, hooks, ledger = owned()
            s.on_spawn = lambda slug, process: hooks.create(slug, active=True)

            await s.forwarders.reconcile()
            await settle()

            assert s.forwarders.is_live("o/mine")
            assert ledger.hooks("o/mine") == {100}
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_a_webhook_left_inactive_by_a_forwarder_that_died_is_deleted_and_the_forwarder_restarts_at_once(
        self,
    ):
        async def scenario():
            s, hooks, ledger = owned()
            spawned = []

            def on_spawn(slug, process):
                spawned.append(process)
                if len(spawned) == 1:  # the first dies right after creating its webhook
                    hooks.create(slug, active=False)
                    process.error = "unable to connect to webhooks server"
                    process.exit(1)
                else:
                    hooks.create(slug, active=True)

            s.on_spawn = on_spawn
            await s.forwarders.reconcile()
            await settle()

            assert hooks.deleted == [("o/mine", 100)]
            assert (
                len(s.processes) == 2
            )  # restarted without waiting out the retry delay
            assert 60 not in s.sleeps
            assert s.forwarders.is_live("o/mine")
            assert s.forwarders.problem("o/mine") is None
            assert ledger.hooks("o/mine") == {101}
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_a_webhook_toolbelt_did_not_create_is_never_deleted(self):
        async def scenario():
            s, hooks, ledger = owned()
            hooks.create("o/mine", active=False)  # somebody's, or of unknown origin

            def on_spawn(slug, process):
                process.error = (
                    "error creating webhook: Hook already exists on this repository"
                )
                process.exit(1)

            s.on_spawn = on_spawn
            await s.forwarders.reconcile()
            await settle()

            problem = s.forwarders.problem("o/mine")
            assert problem is not None and problem.state == "leftover"
            assert "toolbelt did not create" in problem.text
            assert hooks.deleted == []
            assert ledger.hooks("o/mine") == set()
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_a_webhook_with_a_forwarder_connected_that_is_not_ours_is_claimed_elsewhere(
        self,
    ):
        async def scenario():
            s, hooks, ledger = owned()
            hooks.create("o/mine", active=True)  # somebody else's forwarder, connected

            def on_spawn(slug, process):
                process.error = (
                    "error creating webhook: Hook already exists on this repository"
                )
                process.exit(1)

            s.on_spawn = on_spawn
            await s.forwarders.reconcile()
            await settle()

            problem = s.forwarders.problem("o/mine")
            assert problem is not None and problem.state == "claimed"
            assert "not started by toolbelt" in problem.text
            assert hooks.deleted == []
            assert ledger.hooks("o/mine") == set()
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_a_taken_webhook_whose_holder_is_not_known_is_blocked(self):
        async def scenario():
            s = Setup(
                {"o/mine"}, admin={"o/mine"}
            )  # no way to read the repo's webhooks

            def on_spawn(slug, process):
                process.error = (
                    "error creating webhook: Hook already exists on this repository"
                )
                process.exit(1)

            s.on_spawn = on_spawn
            await s.forwarders.reconcile()
            await settle()

            problem = s.forwarders.problem("o/mine")
            assert problem is not None and problem.state == "blocked"
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_a_blocked_start_does_not_claim_a_webhook_that_appeared_meanwhile(self):
        async def scenario():
            s, hooks, ledger = owned()

            def on_spawn(slug, process):
                hooks.create(slug, active=False)  # somebody else won the race
                process.error = (
                    "error creating webhook: Hook already exists on this repository"
                )
                process.exit(1)

            s.on_spawn = on_spawn
            await s.forwarders.reconcile()
            await settle()

            assert ledger.hooks("o/mine") == set()
            assert hooks.deleted == []
            await s.forwarders.stop()

        asyncio.run(scenario())

    def test_an_active_webhook_is_never_deleted_even_if_it_is_ours(self):
        async def scenario():
            s, hooks, ledger = owned()
            hook = hooks.create("o/mine", active=True)
            ledger.add_hook("o/mine", hook.id)

            assert await s.forwarders._reclaim("o/mine") is False

            assert hooks.deleted == []
            assert ledger.hooks("o/mine") == {hook.id}

        asyncio.run(scenario())

    def test_a_recorded_webhook_that_is_gone_is_forgotten(self):
        async def scenario():
            s, _hooks, ledger = owned()
            ledger.add_hook("o/mine", 42)

            assert await s.forwarders._reclaim("o/mine") is False

            assert ledger.hooks("o/mine") == set()

        asyncio.run(scenario())

    def test_nothing_is_deleted_without_a_way_to_list_webhooks(self):
        async def scenario():
            ledger = MemoryLedger()
            ledger.add_hook("o/mine", 5)
            s = Setup({"o/mine"}, admin={"o/mine"}, ledger=ledger)

            assert await s.forwarders._reclaim("o/mine") is False

        asyncio.run(scenario())

    def test_a_github_error_while_listing_does_not_stop_the_forwarder(self):
        async def scenario():
            s, hooks, _ledger = owned()

            async def failing(slug: str):
                raise RuntimeError("GitHub GET failed: 403")

            hooks.list = failing  # type: ignore[method-assign]
            await s.forwarders.reconcile()
            await settle()

            assert len(s.processes) == 1
            assert s.forwarders.is_live("o/mine")
            await s.forwarders.stop()

        asyncio.run(scenario())


class TestForwarderPids:
    def test_the_pid_is_recorded_while_it_runs_and_dropped_when_it_ends(self):
        async def scenario():
            s, _hooks, ledger = owned()
            await s.forwarders.reconcile()
            await settle()
            process = s.processes[0][2]
            assert ledger.pids() == {"o/mine": process.pid}

            await s.forwarders.stop()

            assert ledger.pids() == {}

        asyncio.run(scenario())

    def cleanup(self, commands: dict[int, str | None]):
        ledger = MemoryLedger()
        ledger.set_pid("o/mine", 4242)
        signals: list[tuple[int, int]] = []

        async def process_command(pid: int) -> str | None:
            return commands.get(pid)

        def signal_group(pid: int, sig: int) -> None:
            signals.append((pid, sig))
            if sig == signal.SIGINT:
                commands[pid] = None  # it exits when interrupted

        s = Setup(
            {"o/mine"},
            admin={"o/mine"},
            ledger=ledger,
            process_command=process_command,
            signal_group=signal_group,
        )
        return s, ledger, signals

    def test_a_forwarder_left_by_a_killed_server_is_stopped_at_the_next_start(self):
        async def scenario():
            s, ledger, signals = self.cleanup(
                {
                    4242: "/x/gh-webhook forward --repo=o/mine --events=push --url=http://127.0.0.1:8765/x"
                }
            )

            await s.forwarders.cleanup_orphans()

            assert signals == [(4242, signal.SIGINT)]
            assert ledger.pids() == {}

        asyncio.run(scenario())

    def test_a_pid_that_now_belongs_to_something_else_is_left_alone(self):
        async def scenario():
            s, ledger, signals = self.cleanup({4242: "/usr/bin/python3 some_script.py"})

            await s.forwarders.cleanup_orphans()

            assert signals == []
            assert ledger.pids() == {}

        asyncio.run(scenario())

    def test_a_forwarder_for_another_repo_is_left_alone(self):
        async def scenario():
            s, _ledger, signals = self.cleanup(
                {4242: "/x/gh-webhook forward --repo=o/other --events=push"}
            )

            await s.forwarders.cleanup_orphans()

            assert signals == []

        asyncio.run(scenario())

    def test_a_pid_with_no_process_is_just_dropped(self):
        async def scenario():
            s, ledger, signals = self.cleanup({})

            await s.forwarders.cleanup_orphans()

            assert signals == []
            assert ledger.pids() == {}

        asyncio.run(scenario())

    def test_one_that_ignores_ctrl_c_is_killed(self):
        async def scenario():
            commands: dict[int, str | None] = {
                4242: "/x/gh-webhook forward --repo=o/mine --events=push"
            }
            s, _ledger, signals = self.cleanup(commands)

            def stubborn(pid: int, sig: int) -> None:
                signals.append((pid, sig))
                if sig == signal.SIGKILL:
                    commands[pid] = None

            s.forwarders._signal_group = stubborn

            async def quick(seconds: float) -> None:
                return None

            s.forwarders._sleep = quick
            await s.forwarders.cleanup_orphans()

            assert signals == [(4242, signal.SIGINT), (4242, signal.SIGKILL)]

        asyncio.run(scenario())


def test_the_error_is_kept_when_a_long_usage_text_follows_it():
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
        usage = "".join(f"  --flag{n}  usage line {n}\n" for n in range(60))
        process.stderr.feed_data(
            f"Error: unable to connect to webhooks server, forwarding stopped\nUsage:\n{usage}".encode()
        )
        process.stderr.feed_eof()
        group = _ProcessGroup(process)  # type: ignore[arg-type]
        await settle()

        assert (
            group.last_error()
            == "unable to connect to webhooks server, forwarding stopped"
        )

    asyncio.run(scenario())


class TestRetryDelay:
    """A forwarder that stops is restarted after a short wait that grows while
    it keeps failing, and starts over once one has been up."""

    def setup_forwarders(self, up_for_a_while: list[bool]):
        """Each process it spawns exits at once, or stays up and exits later
        (``up_for_a_while``, one entry per spawn)."""
        waits: list[float] = []
        spawned = []

        async def sleep(seconds: float) -> None:
            if seconds == 5:  # readiness: only elapses for a process that stays up
                if up_for_a_while[len(spawned) - 1]:
                    return
                await asyncio.sleep(3600)
            else:
                waits.append(seconds)

        s = Setup({"o/mine"}, admin={"o/mine"})
        s.forwarders._sleep = sleep
        s.forwarders._retry_initial_seconds = 2
        s.forwarders._retry_seconds = 16

        def on_spawn(slug, process):
            spawned.append(process)
            if not up_for_a_while[len(spawned) - 1]:
                process.exit(1)
            else:
                # up, then dropped a moment later
                asyncio.get_running_loop().call_later(0.01, process.exit, 1)

        s.on_spawn = on_spawn
        return s, waits, spawned

    def test_the_wait_doubles_while_it_keeps_failing_and_stops_at_the_maximum(self):
        async def scenario():
            s, waits, _spawned = self.setup_forwarders([False] * 7)
            await s.forwarders.reconcile()
            for _ in range(60):
                await asyncio.sleep(0)
            await s.forwarders.stop()

            assert waits[:6] == [2, 4, 8, 16, 16, 16]

        asyncio.run(scenario())

    def test_a_forwarder_that_was_up_starts_over_from_the_short_wait(self):
        async def scenario():
            s, waits, _spawned = self.setup_forwarders(
                [False, False, False, True, False]
            )
            await s.forwarders.reconcile()
            for _ in range(80):
                await asyncio.sleep(0.001)
            await s.forwarders.stop()

            # Three quick failures (2, 4, 8), then one that was up and dropped:
            # the wait after it is back to 2.
            assert waits[:5] == [2, 4, 8, 2, 4]

        asyncio.run(scenario())
