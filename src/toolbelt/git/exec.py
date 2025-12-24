import subprocess
from pathlib import Path
from typing import Sequence


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """
    Run a command, raising on failure by default.

    Centralizes subprocess policy so other modules don't reimplement wrappers.
    """
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
    )


def capture(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
) -> str:
    """Run a command and return stdout with surrounding whitespace removed."""
    return subprocess.check_output(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        text=True,
    ).strip()
