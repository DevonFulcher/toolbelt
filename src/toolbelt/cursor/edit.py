import subprocess
import tempfile
from pathlib import Path

import typer

from toolbelt.logger import logger


def edit_text(*, initial_text: str) -> str:
    """
    Open Cursor to edit text and return the edited text.

    Uses `cursor --wait` so this call blocks until the editor is closed.
    """
    with tempfile.TemporaryDirectory(prefix="toolbelt-") as tmp_dir:
        file_path = Path(tmp_dir) / "text.md"
        file_path.write_text(initial_text, encoding="utf-8")

        try:
            subprocess.run(["cursor", "--wait", str(file_path)], check=True)
        except FileNotFoundError as err:
            logger.error(
                "Could not find `cursor` on PATH. Install Cursor CLI or ensure it is available."
            )
            raise typer.Exit(1) from err
        except subprocess.CalledProcessError as err:
            logger.error("Failed to open Cursor editor.")
            raise typer.Exit(1) from err

        return file_path.read_text(encoding="utf-8")
