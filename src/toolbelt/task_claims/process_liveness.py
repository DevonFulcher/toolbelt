"""Is the process that wrote a claim file still running? (macOS and Linux,
this machine only; claim files are local.)

A claim file may record its writer's ``pid`` and ``process_started_at``. The
writer is alive iff a process with that pid exists *and* started at that
time: a bare pid is not enough, since a dead writer's pid can be handed to an
unrelated process.

The check is cheap enough for ``tt ui``'s one-second tick: ``os.kill(pid, 0)``
is a syscall per holder per tick, and the start time (``ps``, the only
portable source) is looked up once per pid and cached for ``CACHE_SECONDS``,
all uncached pids of a read in one ``ps`` call. A pid whose ``os.kill`` fails
is dropped from the cache at once, so a reused pid is looked up afresh.

When liveness cannot be determined (``ps`` missing or failing, a pid that
cannot be probed) the writer counts as alive: the ttl then decides, as for a
file without the fields."""

import os
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from toolbelt import logged_process

#: How long a looked-up start time is trusted before ``ps`` is asked again.
CACHE_SECONDS = 30.0
#: ``ps`` prints whole seconds; the writer's value comes from the same ``ps``.
START_TIME_TOLERANCE = timedelta(seconds=2)

#: ``start time by pid`` for the pids ``ps`` listed, or None if ``ps`` failed.
type StartTimes = dict[int, datetime]
type StartTimeLookup = Callable[[list[int]], StartTimes | None]


def parse_lstart(text: str) -> datetime | None:
    """``ps -o lstart=`` (``Wed Oct 21 14:23:05 2026``, UTC when run with
    ``TZ=UTC``) as an aware datetime."""
    try:
        return datetime.strptime(
            " ".join(text.split()), "%a %b %d %H:%M:%S %Y"
        ).replace(tzinfo=UTC)
    except ValueError:
        return None


def ps_start_times(pids: list[int]) -> StartTimes | None:
    """The start time of each of ``pids`` that exists, from one ``ps`` call
    (None when ``ps`` could not be run)."""
    try:
        result = logged_process.run(
            ["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
        )
    except OSError:
        return None
    found: StartTimes = {}
    for line in result.stdout.splitlines():
        pid_text, _, rest = line.strip().partition(" ")
        started = parse_lstart(rest)
        if pid_text.isdigit() and started is not None:
            found[int(pid_text)] = started
    if not found and result.returncode != 1:
        # ps failing outright (as opposed to "none of these pids exist")
        return None
    return found


class ProcessLiveness:
    def __init__(
        self,
        *,
        signal: Callable[[int, int], None] = os.kill,
        start_times: StartTimeLookup = ps_start_times,
        monotonic: Callable[[], float] = time.monotonic,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._signal = signal
        self._start_times = start_times
        self._monotonic = monotonic
        self._cache_seconds = cache_seconds
        #: pid -> (start time, when it was looked up)
        self._cache: dict[int, tuple[datetime, float]] = {}

    def prime(self, pids: Iterable[int]) -> None:
        """Looks up, in one ``ps`` call, the start time of every live pid in
        ``pids`` that is not cached, so ``is_alive`` spawns nothing."""
        now = self._monotonic()
        self._cache = {
            pid: entry
            for pid, entry in self._cache.items()
            if now - entry[1] < self._cache_seconds
        }
        missing = [
            pid
            for pid in sorted(set(pids))
            if pid not in self._cache and self._exists(pid)
        ]
        if not missing:
            return
        found = self._start_times(missing)
        if found is None:
            return
        for pid, started in found.items():
            self._cache[pid] = (started, now)

    def is_alive(self, pid: int, started_at: datetime) -> bool:
        """Whether ``pid`` exists and started at ``started_at``."""
        if not self._exists(pid):
            self._cache.pop(pid, None)
            return False
        if pid not in self._cache:
            self.prime([pid])
        entry = self._cache.get(pid)
        if entry is None:
            # No start time to compare: unverifiable, or ps no longer lists
            # a pid that existed a moment ago. Unverifiable counts as alive.
            return True
        return abs(entry[0] - started_at) <= START_TIME_TOLERANCE

    def _exists(self, pid: int) -> bool:
        try:
            self._signal(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # EPERM: it exists, owned by someone else. Anything else: cannot tell.
            return True
        return True
