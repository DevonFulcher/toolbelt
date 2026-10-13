"""Atomic file replacement for the claims directory: a reader (the plugin or
``tt ui``) never sees half a file."""

import os
import tempfile
from pathlib import Path

#: Temp files end in this and start with a dot, so readers (which only look at
#: ``<holder>.json``) never mistake one for a claim file.
TEMP_SUFFIX = ".tmp"


def write_atomically(path: Path, text: str) -> None:
    """Writes ``text`` to a temp file in ``path``'s directory, then renames it
    over ``path`` (the directory is created if needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=TEMP_SUFFIX
    )
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
