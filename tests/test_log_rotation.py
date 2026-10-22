"""`tt ui` run by launchd writes its log through a size-capped rotating file, so
a long-running service cannot fill the disk."""

import logging
from pathlib import Path

import pytest

from toolbelt import logger as toolbelt_logger


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_lines_go_to_the_file_with_the_usual_prefix_and_not_to_the_terminal(
    tmp_path: Path, capfd: pytest.CaptureFixture
):
    path = tmp_path / "ui.log"

    toolbelt_logger.log_to_rotating_file(path)
    logging.getLogger("toolbelt").info("hello")
    logging.getLogger().handlers[0].flush()

    assert path.read_text() == "🔧 hello\n"
    assert capfd.readouterr().out == ""


def test_the_file_is_rotated_at_its_size_and_only_a_few_old_ones_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(toolbelt_logger, "LOG_FILE_BYTES", 1000)
    path = tmp_path / "ui.log"

    toolbelt_logger.log_to_rotating_file(path)
    for number in range(500):
        logging.getLogger("toolbelt").info("line %d %s", number, "x" * 40)

    names = sorted(file.name for file in tmp_path.iterdir())
    assert names == ["ui.log", "ui.log.1", "ui.log.2", "ui.log.3"]
    assert all(file.stat().st_size <= 1100 for file in tmp_path.iterdir())
    assert "line 499" in path.read_text()  # the newest lines are in the live file
