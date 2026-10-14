import subprocess
from pathlib import Path

from toolbelt import logged_process


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        logged_process.run(
            ["git", "rev-parse", "--git-dir"],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(path),
        )
        return True
    except subprocess.CalledProcessError:
        return False
