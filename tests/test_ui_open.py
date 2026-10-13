"""`tt ui open`: opens a running `tt ui`, or says how to start one."""

import http.server
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from toolbelt.cli import app

runner = CliRunner()


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def listening() -> Iterator[int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: urls.append(url) or True)
    return urls


def test_open_goes_to_the_running_ui(opened: list[str]):
    with listening() as port:
        result = runner.invoke(app, ["ui", "open", "--port", str(port)])

    assert result.exit_code == 0
    assert opened == [f"http://127.0.0.1:{port}/"]
    assert result.output.strip() == opened[0]


def test_open_can_pick_a_view(opened: list[str]):
    with listening() as port:
        result = runner.invoke(
            app, ["ui", "open", "--port", str(port), "--view", "spend"]
        )

    assert result.exit_code == 0
    assert opened == [f"http://127.0.0.1:{port}/?view=spend"]


def test_open_says_how_to_start_the_ui_when_nothing_listens(opened: list[str]):
    with listening() as port:
        pass  # closed again: nothing listens on `port`

    result = runner.invoke(app, ["ui", "open", "--port", str(port)])

    assert result.exit_code == 1
    assert opened == []
    assert "not listening" in result.output
    assert "launchctl kickstart" in result.output
