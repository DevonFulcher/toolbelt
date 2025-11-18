import subprocess
from pathlib import Path

from toolbelt.env_var import get_env_var_or_exit


def open_in_editor(target: str | Path) -> None:
    """Open the given path in the user's configured editor."""
    editor = get_env_var_or_exit("EDITOR")
    subprocess.run([editor, str(target)], check=True)
