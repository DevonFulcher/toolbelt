"""Runs the sort/filter script's node unit tests (tests/js) from pytest."""

import shutil
import subprocess
from pathlib import Path

import pytest

from toolbelt.ui import api_schema

_JS_TESTS = sorted(Path(__file__).parent.glob("js/*.test.mjs"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_stack_table_script_unit_tests(tmp_path, monkeypatch):
    # The page scripts draw from the state catalog; the tests get the same
    # JSON the page embeds.
    catalog = tmp_path / "state-catalog.json"
    catalog.write_text(api_schema.catalog_json())
    monkeypatch.setenv("TT_STATE_CATALOG", str(catalog))

    result = subprocess.run(
        ["node", "--test", *map(str, _JS_TESTS)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
