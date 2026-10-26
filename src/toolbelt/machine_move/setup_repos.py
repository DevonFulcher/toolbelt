"""Getting the restored checkouts ready to use: the tool versions and the
Python environment of each, which `tt machine import` does not create (a clone
is only sources).

What is run follows what the checkout has: ``asdf install`` for a
``.tool-versions`` and ``uv sync`` for a ``uv.lock``. A command that fails is
reported, and the next one still runs.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from toolbelt import logged_process

type Runner = Callable[[Path, list[str]], tuple[int, str]]


@dataclass(frozen=True)
class SetupResult:
    path: Path
    command: str
    ok: bool
    detail: str = ""


def commands_for(checkout: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    if (checkout / ".tool-versions").is_file():
        commands.append(["asdf", "install"])
    if (checkout / "uv.lock").is_file():
        commands.append(["uv", "sync"])
    return commands


def _run(checkout: Path, command: list[str]) -> tuple[int, str]:
    try:
        result = logged_process.run(
            command, cwd=str(checkout), capture_output=True, text=True
        )
    except FileNotFoundError:
        return 127, f"{command[0]} is not installed"
    return result.returncode, (result.stdout + result.stderr).strip()


def set_up(checkouts: list[Path], runner: Runner = _run) -> list[SetupResult]:
    """Runs each checkout's setup commands; checkouts that are not there are
    skipped."""
    results: list[SetupResult] = []
    for checkout in checkouts:
        if not checkout.is_dir():
            continue
        for command in commands_for(checkout):
            code, output = runner(checkout, command)
            results.append(
                SetupResult(
                    checkout,
                    " ".join(command),
                    code == 0,
                    "" if code == 0 else (output.splitlines() or ["failed"])[-1],
                )
            )
    return results
