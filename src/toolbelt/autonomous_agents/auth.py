"""Whether Claude Code is signed in (what a run needs)."""

import json

from toolbelt.git.exec import run


def claude_signed_in() -> bool:
    """``claude auth status`` says it is signed in. A run's login can still
    be found expired when it starts; that shows as a failed run."""
    result = run(["claude", "auth", "status"], capture_output=True, check=False)
    try:
        return bool(json.loads(result.stdout).get("loggedIn"))
    except ValueError:
        return False
