"""The web client's API types are generated from the Python wire models
(``toolbelt.ui.api_models``); these tests fail when the committed generated
files are stale. Fix with ``pnpm --dir web gen:types``."""

import shutil
import subprocess
from pathlib import Path

import pytest

from toolbelt.ui import api_schema

WEB = Path(__file__).parent.parent / "web"
API_DIR = WEB / "src" / "api"
REGENERATE = "stale: run `pnpm --dir web gen:types` and commit the result"


def test_the_committed_json_schema_is_current():
    assert (API_DIR / "schema.json").read_text() == api_schema.schema_json(), REGENERATE


def test_the_committed_state_catalog_is_current():
    assert (API_DIR / "catalog.json").read_text() == api_schema.catalog_json(), (
        REGENERATE
    )


@pytest.mark.skipif(
    shutil.which("pnpm") is None or not (WEB / "node_modules").is_dir(),
    reason="pnpm or the web client's dependencies are not installed",
)
def test_the_committed_typescript_types_are_current():
    result = subprocess.run(
        ["pnpm", "--dir", str(WEB), "check:types"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, REGENERATE + "\n" + result.stdout + result.stderr
