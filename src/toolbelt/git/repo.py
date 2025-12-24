import subprocess
from pathlib import Path


def get_current_repo_root_path() -> Path:
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def current_repo_org() -> str:
    """Get the GitHub organization name for the current repository."""
    try:
        # Get the remote URL for origin
        remote_url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Handle both HTTPS and SSH URLs
        if remote_url.startswith("https://"):
            # Format: https://github.com/org/repo.git
            parts = remote_url.split("/")
            return parts[-2]
        else:
            # Format: git@github.com:org/repo.git
            parts = remote_url.split(":")
            return parts[1].split("/")[0]
    except subprocess.CalledProcessError as err:
        raise ValueError(
            "Failed to get remote URL. Is this a git repository with a remote?"
        ) from err
