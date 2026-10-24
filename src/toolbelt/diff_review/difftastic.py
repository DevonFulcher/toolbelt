"""difftastic's structural diff of one file, as its JSON output."""

import os
import tempfile
from pathlib import Path, PurePosixPath

from toolbelt import logged_process
from toolbelt.ui import api_models as api


def structural_diff(path: str, *, old_text: str, new_text: str) -> api.DifftasticFile:
    """Diffs the two texts with ``difft``. The temp files keep ``path``'s file
    name because difftastic picks the language from its extension."""
    name = PurePosixPath(path).name
    with tempfile.TemporaryDirectory() as tmp:
        old = Path(tmp) / "old" / name
        new = Path(tmp) / "new" / name
        for file, text in ((old, old_text), (new, new_text)):
            file.parent.mkdir()
            file.write_text(text)
        result = logged_process.run(
            ["difft", "--display", "json", "--color", "never", str(old), str(new)],
            capture_output=True,
            check=True,
            # The JSON output is not yet a stable interface; this opts in.
            env={**os.environ, "DFT_UNSTABLE": "yes"},
        )
    return api.DifftasticFile.model_validate_json(result.stdout)
