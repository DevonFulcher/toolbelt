"""Run the UI under uvicorn, bound to loopback only."""

import socket
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import quote

import typer
import uvicorn

from toolbelt.ui.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def serve(*, port: int, cwd_root: Path | None) -> None:
    """Serve until interrupted. ``port`` 0 picks a free one; the URL is
    printed once the socket is bound, so it is always the real one."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    typer.echo(f"http://{HOST}:{sock.getsockname()[1]}/")
    # Open event streams never finish on their own: without a grace period,
    # Ctrl-C would wait on every connected browser.
    config = uvicorn.Config(
        create_app(cwd_root), log_level="warning", timeout_graceful_shutdown=1
    )
    server = uvicorn.Server(config)
    server.run(sockets=[sock])


def open_in_browser(*, port: int, view: str = "") -> str:
    """Open the ``tt ui`` listening on ``port`` (at ``view``, if given) in the
    default browser and return its URL. Exits with a hint when nothing is
    listening there."""
    base = f"http://{HOST}:{port}/"
    try:
        urllib.request.urlopen(base, timeout=2).close()
    except OSError as error:
        typer.echo(
            f"tt ui is not listening on port {port}. Start it with `tt ui`, or "
            "restart the login service: "
            "`launchctl kickstart -k gui/$(id -u)/com.user.toolbelt.ui`.",
            err=True,
        )
        raise typer.Exit(1) from error
    url = f"{base}?view={quote(view)}" if view else base
    webbrowser.open(url)
    return url
