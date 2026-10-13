"""Runs the web client's own checks (web/, React + TypeScript) from pytest:
Vitest (the client's logic and component tests), the TypeScript type check
(which is also where the generated API types are held to the client's code)
and ESLint. Skipped where pnpm or the client's dependencies are not
installed (`pnpm --dir web install`)."""

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).parent.parent / "web"

pytestmark = pytest.mark.skipif(
    shutil.which("pnpm") is None or not (WEB / "node_modules").is_dir(),
    reason="pnpm or the web client's dependencies are not installed",
)


@pytest.mark.parametrize("script", ["test", "typecheck", "lint"])
def test_web_client_check(script):
    result = subprocess.run(
        ["pnpm", "--dir", str(WEB), script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
