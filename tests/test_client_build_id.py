"""Which build of the web client the server serves: a page left open across an
update compares it with its own to say it is out of date."""

import asyncio
import json
from pathlib import Path

from toolbelt.ui.app import create_app
from toolbelt.ui.client_build import served_build


def write_index(dist: Path, script: str) -> None:
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(
        f'<html><head><script type="module" crossorigin src="/assets/{script}"></script>'
        '<link rel="stylesheet" href="/assets/index-abc.css"></head></html>'
    )


def test_the_build_is_the_main_scripts_file_name(tmp_path: Path):
    write_index(tmp_path, "index-C2k9uj7O.js")

    assert served_build(tmp_path) == "index-C2k9uj7O.js"


def test_a_new_build_has_a_different_name(tmp_path: Path):
    write_index(tmp_path, "index-old.js")
    before = served_build(tmp_path)
    write_index(tmp_path, "index-new.js")

    assert served_build(tmp_path) != before


def test_there_is_no_build_without_a_built_index(tmp_path: Path):
    assert served_build(tmp_path / "missing") is None
    (tmp_path / "index.html").write_text("<html></html>")
    assert served_build(tmp_path) is None


def test_the_hello_event_says_which_build_is_served(tmp_path: Path):
    write_index(tmp_path / "dist", "index-served.js")
    app = create_app(client_dist=tmp_path / "dist")
    events = next(
        route for route in app.routes if getattr(route, "path", "") == "/api/events"
    )

    async def first_event() -> str:
        response = await events.endpoint()  # type: ignore[attr-defined]
        try:
            return await response.body_iterator.__anext__()
        finally:
            await response.body_iterator.aclose()

    event = asyncio.run(first_event())

    assert event.startswith("event: hello")
    assert json.loads(event.split("data: ", 1)[1])["build"] == "index-served.js"
