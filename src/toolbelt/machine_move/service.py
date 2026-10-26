"""The `tt ui` launchd service, which the export stops.

While it runs it writes the DB (agents, Slack, decisions) and keeps the repos'
one webhook forwarder: GitHub allows one per repo, so the old laptop's would
block the new laptop's. Stopping it before the DB is copied also means nothing
changes after the snapshot. It is not started again after a successful export:
the old laptop is on its way out.
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Protocol

from toolbelt import logged_process

SERVICE_LABEL = "com.user.toolbelt.ui"
#: A forwarder started by the service replays events to its own port.
_FORWARDER_PATTERN = "webhook forward .*--url=http://127.0.0.1"
_WAIT_SECONDS = 10.0


class ServiceControl(Protocol):
    def stop(self) -> str:
        """Stops the service and its webhook forwarders; returns a line saying
        what was done."""
        ...

    def start(self) -> None:
        """Starts it again (the export failed after stopping it)."""
        ...


class LaunchdService:
    def __init__(self, *, home: Path | None = None, label: str = SERVICE_LABEL) -> None:
        self._home = home or Path.home()
        self._label = label
        self._domain = f"gui/{os.getuid()}"

    def _launchctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return logged_process.run(["launchctl", *args], capture_output=True, text=True)

    def _loaded(self) -> bool:
        return self._launchctl("print", f"{self._domain}/{self._label}").returncode == 0

    @staticmethod
    def _forwarders() -> list[int]:
        result = logged_process.run(
            ["pgrep", "-f", _FORWARDER_PATTERN], capture_output=True, text=True
        )
        return [int(pid) for pid in result.stdout.split()]

    def stop(self) -> str:
        if not self._loaded():
            return f"{self._label} was not running"
        self._launchctl("bootout", f"{self._domain}/{self._label}")
        deadline = time.monotonic() + _WAIT_SECONDS
        while self._forwarders() and time.monotonic() < deadline:
            time.sleep(0.5)
        left = self._forwarders()
        for pid in left:
            # SIGINT, as the service does: the extension then deletes its webhook.
            os.kill(pid, 2)
        return (
            f"stopped {self._label} and its webhook forwarders"
            + (f" (ended {len(left)} that were left)" if left else "")
            + ". Start it again with: launchctl bootstrap "
            f"{self._domain} ~/Library/LaunchAgents/{self._label}.plist"
        )

    def start(self) -> None:
        plist = self._home / "Library" / "LaunchAgents" / f"{self._label}.plist"
        self._launchctl("bootstrap", self._domain, str(plist))
