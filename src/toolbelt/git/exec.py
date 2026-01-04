import subprocess
from pathlib import Path
from typing import Sequence

import typer

from toolbelt.logger import logger


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    exit_on_error: bool = False,
) -> subprocess.CompletedProcess[str]:
    """
    Run a command with consistent logging and error-handling policy.

    Centralizes subprocess policy so other modules don't reimplement wrappers.

    Args:
        cmd: Command and arguments (e.g. ["git", "status"]).
        cwd: Working directory to run the command from.
        check: If True, raise `subprocess.CalledProcessError` on non-zero exit.
        exit_on_error: If True, convert failures into `typer.Exit` for CLI-friendly
            termination (avoids a traceback). This applies both when `check=True`
            (exception path) and when `check=False` (non-zero return code path).

    Notes:
        Common combinations:
        - check=True,  exit_on_error=False: propagate `CalledProcessError` (traceback).
        - check=True,  exit_on_error=True:  `typer.Exit(1)` on failure.
        - check=False, exit_on_error=False: never raise; caller inspects `returncode`.
        - check=False, exit_on_error=True:  `typer.Exit(returncode)` on failure.
    """
    logger.info(" ".join(cmd))

    try:
        result = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            check=check,
            text=True,
        )
    except subprocess.CalledProcessError as err:
        logger.error(f"Command failed: {' '.join(cmd)}")
        if exit_on_error:
            raise typer.Exit(err.returncode) from err
        raise

    if check is False and exit_on_error and result.returncode != 0:
        logger.error(f"Command failed: {' '.join(cmd)}")
        raise typer.Exit(result.returncode)

    return result


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
