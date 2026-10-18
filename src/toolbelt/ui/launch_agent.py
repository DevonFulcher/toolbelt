"""The `tt ui` login service (the dotfiles `com.user.toolbelt.ui` LaunchAgent)."""

import os
from collections.abc import Callable
from typing import Any

from toolbelt import logged_process

UI_LAUNCH_AGENT_LABEL = "com.user.toolbelt.ui"


def restart_ui_launch_agent(
    say: Callable[[str], None], run: Callable[..., Any] = logged_process.run
) -> bool:
    """Restart the `tt ui` LaunchAgent so it serves the code now on disk.

    Does nothing when the agent is not loaded (a `tt ui` started by hand is left
    alone). Returns whether it restarted the agent.
    """
    service = f"gui/{os.getuid()}/{UI_LAUNCH_AGENT_LABEL}"
    if run(["launchctl", "print", service], capture_output=True).returncode != 0:
        return False
    run(["launchctl", "kickstart", "-k", service], check=True)
    say(f"restarted {UI_LAUNCH_AGENT_LABEL}")
    return True
