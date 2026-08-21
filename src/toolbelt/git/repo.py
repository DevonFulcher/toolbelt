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
    """Get the GitHub organization name for the current repository.

    Returns "" if there's no origin remote or it isn't GitHub-shaped (e.g. a
    local path, as in throwaway test repos) — the sole caller (store_commit)
    uses this for best-effort bookkeeping, not to gate real behavior, so a
    missing org shouldn't crash the command that triggered it.
    """
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    remote_url = result.stdout.strip()

    # Handle both HTTPS and SSH URLs
    if remote_url.startswith("https://"):
        # Format: https://github.com/org/repo.git
        parts = remote_url.split("/")
        return parts[-2] if len(parts) >= 2 else ""
    # Format: git@github.com:org/repo.git
    parts = remote_url.split(":")
    return parts[1].split("/")[0] if len(parts) >= 2 else ""
