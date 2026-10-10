"""Runs the sort/filter script's node unit tests (tests/js) from pytest."""

import shutil
import subprocess
from pathlib import Path

import pytest

_JS_TESTS = sorted(Path(__file__).parent.glob("js/*.test.mjs"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_stack_table_script_unit_tests():
    result = subprocess.run(
        ["node", "--test", *map(str, _JS_TESTS)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
