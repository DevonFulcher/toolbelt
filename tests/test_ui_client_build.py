"""When `tt ui` builds the web client, and what it says when it cannot. The
tool lookup and the process runner are injected: nothing here runs pnpm."""

import os
import subprocess
from pathlib import Path

import pytest

from toolbelt.ui.client_build import ClientBuildError, build_client, is_stale


class Runner:
    """A stand-in for `logged_process.run` that records commands and can
    make one fail."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.commands: list[tuple[list[str], Path]] = []
        self.fail_on = fail_on

    def __call__(self, command, *, cwd, **_):
        self.commands.append((command, cwd))
        failed = self.fail_on is not None and self.fail_on in command
        return subprocess.CompletedProcess(
            command, 1 if failed else 0, "out", "error: boom" if failed else ""
        )


def tools(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


@pytest.fixture
def web(tmp_path: Path) -> tuple[Path, Path]:
    web_dir, dist = tmp_path / "web", tmp_path / "dist"
    (web_dir / "src" / "components").mkdir(parents=True)
    (web_dir / "package.json").write_text("{}")
    (web_dir / "pnpm-lock.yaml").write_text("lock")
    (web_dir / "src" / "main.tsx").write_text("main")
    (web_dir / "src" / "components" / "App.tsx").write_text("app")
    return web_dir, dist


def touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x")
    os.utime(path, (mtime, mtime))


def build_at(dist: Path, mtime: float) -> None:
    touch(dist / "index.html", mtime)


class TestIsStale:
    def test_a_missing_build_is_stale(self, web):
        web_dir, dist = web

        assert is_stale(web_dir, dist)

    def test_a_build_newer_than_every_input_is_fresh(self, web):
        web_dir, dist = web
        for path in web_dir.rglob("*"):
            if path.is_file():
                touch(path, 1000)
        build_at(dist, 2000)

        assert not is_stale(web_dir, dist)

    @pytest.mark.parametrize(
        "changed",
        ["package.json", "pnpm-lock.yaml", "src/main.tsx", "src/components/App.tsx"],
    )
    def test_a_newer_source_makes_it_stale(self, web, changed):
        web_dir, dist = web
        for path in web_dir.rglob("*"):
            if path.is_file():
                touch(path, 1000)
        build_at(dist, 2000)

        touch(web_dir / changed, 3000)

        assert is_stale(web_dir, dist)

    def test_a_new_file_under_src_makes_it_stale(self, web):
        web_dir, dist = web
        for path in web_dir.rglob("*"):
            if path.is_file():
                touch(path, 1000)
        build_at(dist, 2000)

        touch(web_dir / "src" / "components" / "New.tsx", 3000)

        assert is_stale(web_dir, dist)

    def test_tests_are_not_part_of_the_build(self, web):
        web_dir, dist = web
        for path in web_dir.rglob("*"):
            if path.is_file():
                touch(path, 1000)
        build_at(dist, 2000)

        touch(web_dir / "src" / "components" / "App.test.tsx", 3000)
        touch(web_dir / "src" / "testing" / "fixtures.ts", 3000)

        assert not is_stale(web_dir, dist)


class TestBuildClient:
    def test_installs_the_locked_dependencies_then_builds(self, web):
        web_dir, dist = web
        run, said = Runner(), []

        built = build_client(
            web_dir=web_dir,
            dist_dir=dist,
            which=tools("node", "pnpm"),
            run=run,
            say=said.append,
        )

        assert built is True
        assert run.commands == [
            (["pnpm", "install", "--frozen-lockfile"], web_dir),
            (["pnpm", "build"], web_dir),
        ]
        assert said == ["building the web client (web/)…"]

    def test_does_nothing_when_the_build_is_fresh(self, web):
        web_dir, dist = web
        for path in web_dir.rglob("*"):
            if path.is_file():
                touch(path, 1000)
        build_at(dist, 2000)
        run = Runner()

        built = build_client(
            web_dir=web_dir, dist_dir=dist, which=tools(), run=run, say=print
        )

        assert built is False
        assert run.commands == []

    def test_force_builds_a_fresh_build_anyway(self, web):
        web_dir, dist = web
        for path in web_dir.rglob("*"):
            if path.is_file():
                touch(path, 1000)
        build_at(dist, 2000)
        run = Runner()

        built = build_client(
            force=True,
            web_dir=web_dir,
            dist_dir=dist,
            which=tools("node", "pnpm"),
            run=run,
            say=print,
        )

        assert built is True
        assert len(run.commands) == 2

    @pytest.mark.parametrize("missing", ["node", "pnpm"])
    def test_a_missing_tool_is_a_one_line_error(self, web, missing):
        web_dir, dist = web
        present = {"node", "pnpm"} - {missing}
        run = Runner()

        with pytest.raises(ClientBuildError) as error:
            build_client(
                web_dir=web_dir,
                dist_dir=dist,
                which=tools(*present),
                run=run,
                say=print,
            )

        assert missing in str(error.value)
        assert "\n" not in str(error.value)
        assert run.commands == []

    def test_a_failed_build_shows_the_tool_output(self, web):
        web_dir, dist = web

        with pytest.raises(ClientBuildError, match=r"(?s)pnpm build.*error: boom"):
            build_client(
                web_dir=web_dir,
                dist_dir=dist,
                which=tools("node", "pnpm"),
                run=Runner(fail_on="build"),
                say=print,
            )

    def test_sources_that_are_not_there_are_explained(self, tmp_path):
        with pytest.raises(ClientBuildError, match="sources are not at"):
            build_client(
                web_dir=tmp_path / "nope",
                dist_dir=tmp_path / "dist",
                which=tools("node", "pnpm"),
                run=Runner(),
                say=print,
            )
