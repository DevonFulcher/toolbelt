"""Snapshot tests for user-facing CLI output.

Complements the value-based assertions elsewhere: instead of asserting a
message contains some substring, this captures the exact text a user sees and
diffs future runs against a committed snapshot file
(tests/__snapshots__/test_snapshots.ambr). Reviewing a change here means
reading a snapshot diff, not new assertion code — accept intentional wording
changes with `pytest --snapshot-update`.

Only `git tree`'s output is snapshotted: it's pure, deterministic text (branch
names + ASCII tree characters, no paths/timestamps), unlike e.g. `git diff`
output which is data-specific and not a meaningful thing to pin.
"""

from pathlib import Path

import pytest
from syrupy.assertion import SnapshotAssertion

from test_cli import _invoke

from toolbelt.git.stack.append import create_stacked_branch


def test_tree_output_empty_stack(
    repo: Path, capfd: pytest.CaptureFixture, snapshot: SnapshotAssertion
):
    result = _invoke(["tree"], cwd=repo, capfd=capfd)
    assert result.output == snapshot


def test_tree_output_branching_stack(
    repo: Path,
    tmp_path: Path,
    capfd: pytest.CaptureFixture,
    snapshot: SnapshotAssertion,
):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    create_stacked_branch("api_tests", root=api_wt, wt_path=tmp_path / "wt-api-tests")
    create_stacked_branch("api_docs", root=api_wt, wt_path=tmp_path / "wt-api-docs")

    result = _invoke(["tree"], cwd=repo, capfd=capfd)

    assert result.output == snapshot
