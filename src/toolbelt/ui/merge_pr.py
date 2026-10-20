"""Lands a PR from the page by running `toolbelt git merge`, exactly as typing
it would: squash-merge on GitHub, then sync the stack the merged branch belonged
to."""

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from toolbelt import logged_process

#: Longest output kept, the end of it (where an error is).
OUTPUT_LIMIT = 4000


@dataclass(frozen=True)
class MergeOutcome:
    ok: bool
    output: str


def run_toolbelt_merge(repo_path: Path, pr_url: str) -> MergeOutcome:
    """Run `toolbelt git merge <pr_url>` in the repo at ``repo_path``."""
    executable = shutil.which("toolbelt") or sys.argv[0]
    try:
        result = logged_process.run(
            [executable, "git", "merge", pr_url],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as error:  # noqa: BLE001 - reported to the page
        return MergeOutcome(ok=False, output=f"{type(error).__name__}: {error}")
    output = (result.stdout + result.stderr).strip()
    return MergeOutcome(ok=result.returncode == 0, output=output[-OUTPUT_LIMIT:])
