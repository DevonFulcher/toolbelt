"""Builds the web client (``web/``, React + TypeScript) into the directory
``tt ui`` serves it from.

``tt ui`` builds it when the build is missing or older than its sources, and
``tt upgrade`` always does: ``pnpm install --frozen-lockfile`` then
``pnpm build`` (Vite writes ``src/toolbelt/ui/static/dist``, which is
gitignored). The build needs node and pnpm; their absence is a one-line
error, not a traceback.

The tool lookup and the process runner are arguments so tests drive the
decisions without running pnpm.
"""

import re
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from toolbelt import logged_process

#: The built client: ``index.html`` plus content-hashed ``assets/``.
CLIENT_DIST = Path(__file__).parent / "static" / "dist"
#: The client's sources, next to ``src/`` in the repo (an editable install,
#: which is how `tt` is installed, keeps them reachable).
WEB_DIR = Path(__file__).resolve().parents[3] / "web"

# What the build reads, besides ``src/``.
_INPUT_FILES = (
    "package.json",
    "pnpm-lock.yaml",
    "index.html",
    "vite.config.ts",
    "tsconfig.json",
)


def served_build(dist: Path = CLIENT_DIST) -> str | None:
    """Which build of the client ``dist`` holds: the file name of its main script
    (content-hashed, so it changes with every build), or ``None`` when there is
    no build. A page compares it with its own to say it is out of date."""
    try:
        page = (dist / "index.html").read_text()
    except OSError:
        return None
    found = re.search(r"/assets/(index-[\w-]+\.js)", page)
    return found.group(1) if found else None


class ClientBuildError(Exception):
    """The client cannot be built; the message says why in one line."""


def _inputs(web_dir: Path) -> Iterator[Path]:
    for name in _INPUT_FILES:
        path = web_dir / name
        if path.is_file():
            yield path
    for path in (*(web_dir / "src").rglob("*"), *(web_dir / "public").rglob("*")):
        # Tests are not part of the build.
        if not path.is_file() or ".test." in path.name or "testing" in path.parts:
            continue
        yield path


def is_stale(web_dir: Path = WEB_DIR, dist_dir: Path = CLIENT_DIST) -> bool:
    """Whether the build is missing or older than any file it is made from."""
    index = dist_dir / "index.html"
    if not index.is_file():
        return True
    built = index.stat().st_mtime
    return any(path.stat().st_mtime > built for path in _inputs(web_dir))


def build_client(
    *,
    force: bool = False,
    web_dir: Path = WEB_DIR,
    dist_dir: Path = CLIENT_DIST,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = logged_process.run,
    say: Callable[[str], None] = print,
) -> bool:
    """Builds the client when it is stale (or ``force``). Returns whether a
    build ran. Raises ``ClientBuildError`` (one line) when it cannot."""
    if not force and not is_stale(web_dir, dist_dir):
        return False
    if not (web_dir / "package.json").is_file():
        raise ClientBuildError(
            f"the web client's sources are not at {web_dir}: install toolbelt "
            "from its repository (uv tool install --editable .)"
        )
    for tool in ("node", "pnpm"):
        if which(tool) is None:
            raise ClientBuildError(
                f"building the web client needs {tool}, which is not on PATH "
                "(install node and pnpm, then run `tt ui` again)"
            )
    say("building the web client (web/)…")
    for command in (["pnpm", "install", "--frozen-lockfile"], ["pnpm", "build"]):
        result = run(command, cwd=web_dir, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            raise ClientBuildError(
                f"`{' '.join(command)}` failed in {web_dir}:\n{output.strip()}"
            )
    return True
