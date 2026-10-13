"""`tt` start-up cost: importing the CLI must not load the heavy libraries.

They are imported with `lazy import` (PEP 810) and only needed by the commands
that use them; an eager import would add over a second to every invocation.
"""

import subprocess
import sys

HEAVY = ["pandas", "matplotlib", "seaborn", "boto3", "pydantic", "httpx", "yaml"]


def test_importing_the_cli_does_not_load_heavy_libraries():
    script = (
        "import sys\n"
        "import toolbelt.cli\n"
        f"loaded = [m for m in {HEAVY!r} if m in sys.modules]\n"
        "print(','.join(loaded))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == ""
