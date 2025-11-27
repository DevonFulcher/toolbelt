import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

import boto3
import toml
import typer
import yaml

from toolbelt.git.commits import store_commit
from toolbelt.logger import logger
from toolbelt.repos import current_repo, current_repo_name

DefaultBranchName = Literal["main", "master", "current"]

DEFAULT_BRANCH_NAMES: tuple[DefaultBranchName, ...] = (
    "main",
    "master",
    "current",
)


def get_default_branch() -> DefaultBranchName:
    for branch_name in DEFAULT_BRANCH_NAMES:
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", branch_name],
                check=True,
                capture_output=True,
                text=True,
            )
            return branch_name
        except subprocess.CalledProcessError:
            continue

    formatted_names = ", ".join(f"'{name}'" for name in DEFAULT_BRANCH_NAMES)
    logger.error(
        f"None of the default branches {formatted_names} exist.",
    )
    raise typer.Exit(1)


def get_current_branch_name() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def get_parent_branch_name(child_branch_name: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", f"{child_branch_name}@{{u}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def update_repo(target_path: Path):
    if (target_path / ".tool-versions").exists():
        # This may fail if the plugins in .tool-versions are not installed
        subprocess.run(["asdf", "install"], check=True)
    if (target_path / "uv.lock").exists():
        subprocess.run(["uv", "sync"], check=True)


def sync_repo():
    # Run git-town continue in case a git conflict happened during the last save
    subprocess.run(["git-town", "continue"], check=True)
    subprocess.run(["git-town", "sync", "--stack"], check=True)
    update_repo(get_current_repo_root_path())
    subprocess.run(["git", "push"], check=True)


def git_pr(skip_tests: bool) -> None:
    view_pr = subprocess.run(["gh", "pr", "view", "--web"], check=False)
    if view_pr.returncode == 0:
        return
    repo = current_repo()
    if repo and not skip_tests:
        repo.check()
    subprocess.run(
        ["gh", "pr", "create", "--web"],
        check=False,
    )


def git_branch_clean() -> None:
    """
    Delete local branches whose upstream has been removed.
    """
    subprocess.run(["git", "fetch", "-p"], check=True)
    branch_list = subprocess.run(
        ["git", "branch", "-vv"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    repo_root = get_current_repo_root_path()
    deleted_branches: list[str] = []
    for line in branch_list:
        if ": gone]" not in line:
            continue
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] in {"*", "+"}:
            if len(tokens) < 2:
                continue
            branch_name = tokens[1]
        else:
            branch_name = tokens[0]
        delete_branch_and_worktree(branch_name, repo_root=repo_root)
        deleted_branches.append(branch_name)

    if deleted_branches:
        logger.info("Deleted branches:")
        for branch_name in deleted_branches:
            logger.info(f"  {branch_name}")
    else:
        logger.info("No branches to delete.")


def _worktree_entries(root: Path) -> list[tuple[Path, str | None]]:
    """
    Return the registered git worktrees and their associated branch names.

    Parameters
    ----------
    root:
        Path to the repository root.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    )

    entries: list[tuple[Path, str | None]] = []
    current_path: Path | None = None
    current_branch: str | None = None
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current_path is not None:
                entries.append((current_path, current_branch))
            current_path = None
            current_branch = None
            continue

        key, _, value = line.partition(" ")
        if key == "worktree":
            current_path = Path(value)
        elif key == "branch":
            current_branch = value.removeprefix("refs/heads/")
        elif key == "detached":
            current_branch = None

    if current_path is not None:
        entries.append((current_path, current_branch))

    return entries


def _worktree_paths_for_branch(branch_name: str, root: Path) -> list[Path]:
    """
    Return the list of worktree paths that are currently checked out to the
    provided branch.
    """
    paths = [
        path
        for path, branch in _worktree_entries(root)
        if branch is not None and branch == branch_name
    ]

    unique_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for path in paths:
        if path not in seen_paths:
            unique_paths.append(path)
            seen_paths.add(path)

    return unique_paths


def delete_branch_and_worktree(
    branch_name: str,
    *,
    repo_root: Path | None = None,
    force: bool = False,
) -> None:
    """
    Delete a local branch and its associated worktree (if present).

    Parameters
    ----------
    branch_name:
        The name of the branch to delete.
    repo_root:
        Optional path to the repository root. If omitted, the current repository
        root is detected automatically.
    force:
        If True, pass ``--force`` to ``git worktree remove``.
    """
    root = repo_root or get_current_repo_root_path()
    branch_to_delete = branch_name

    def branch_exists(name: str) -> bool:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
            cwd=root,
            check=False,
        )
        return result.returncode == 0

    candidates: list[str] = []
    candidates.append(branch_name)
    bare_name = branch_name.removeprefix("devon/")
    if branch_name == bare_name:
        candidates.append(f"devon/{bare_name}")
    else:
        candidates.append(bare_name)

    for candidate in candidates:
        if candidate and branch_exists(candidate):
            branch_to_delete = candidate
            break

    worktree_paths = _worktree_paths_for_branch(branch_to_delete, root)

    for path in worktree_paths:
        cmd = ["git", "worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(str(path))
        logger.info(" ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=root,
            check=False,
        )
        if result.stdout:
            logger.info(result.stdout.rstrip())
        if result.returncode != 0:
            if result.stderr:
                logger.error(result.stderr.rstrip())
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
        logger.info(f"Removed {path}")

    # Clean up any stale worktree references so branch deletion succeeds.
    subprocess.run(["git", "worktree", "prune"], check=True, cwd=root)

    logger.info(f"git branch -D {branch_to_delete}")
    branch_delete = subprocess.run(
        ["git", "branch", "-D", branch_to_delete],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    if branch_delete.stdout:
        logger.info(branch_delete.stdout.rstrip())
    if branch_delete.returncode != 0:
        if branch_delete.stderr:
            logger.error(branch_delete.stderr.rstrip())
        raise subprocess.CalledProcessError(
            branch_delete.returncode,
            branch_delete.args,
            output=branch_delete.stdout,
            stderr=branch_delete.stderr,
        )
    logger.info(f"Deleted branch {branch_to_delete}")


def check_for_parent_branch_merge_conflicts(
    *, current_branch: str, default_branch: str
) -> None:
    logger.info("Checking for merge conflicts with parent branch")
    try:
        parent_branch = get_parent_branch_name(current_branch)
    except subprocess.CalledProcessError:
        if current_branch == default_branch:
            # Default branch without upstream - this is unusual but can happen
            logger.warning(f"Warning: {default_branch} has no upstream branch set")
            raise typer.Exit(1)
        else:
            # Regular branch without upstream - offer to set it
            logger.info(f"Branch '{current_branch}' has no upstream branch set")
            should_set_upstream = input(
                "Would you like to set an upstream branch? (y/n): "
            )
            if should_set_upstream.lower() == "y":
                try:
                    # Try to set upstream to origin/branch_name
                    subprocess.run(
                        [
                            "git",
                            "branch",
                            "--set-upstream-to",
                            f"origin/{current_branch}",
                            current_branch,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    logger.info(f"Set upstream branch to origin/{current_branch}")
                    # Try conflict check again with newly set upstream
                    parent_branch = get_parent_branch_name(current_branch)
                except subprocess.CalledProcessError:
                    # If setting upstream failed (e.g., remote branch doesn't exist)
                    logger.warning(
                        "Failed to set upstream branch - remote branch may not exist"
                    )
                    should_push = input(
                        "Would you like to push and set upstream now? (y/n): "
                    )
                    if should_push.lower() == "y":
                        try:
                            subprocess.run(
                                [
                                    "git",
                                    "push",
                                    "--set-upstream",
                                    "origin",
                                    current_branch,
                                ],
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                            logger.info(
                                f"Pushed and set upstream to origin/{current_branch}"
                            )
                            # Try conflict check one final time
                            parent_branch = get_parent_branch_name(current_branch)
                        except subprocess.CalledProcessError:
                            logger.error("Failed to push and set upstream branch")
                            raise typer.Exit(1)
                    else:
                        raise typer.Exit(1)
            else:
                raise typer.Exit(1)

    if parent_branch:
        try:
            merge_tree_result = subprocess.run(
                ["git", "merge-tree", parent_branch, current_branch],
                capture_output=True,
                text=True,
                check=False,
            )
            if "changed in both" in merge_tree_result.stdout:
                logger.warning(
                    "⚠️  Warning: This commit may create merge conflicts with the parent branch."
                )
                proceed = input("Do you want to continue anyway? (y/n): ")
                if proceed.lower() != "y":
                    # Unstage changes if user aborts
                    subprocess.run(["git", "reset"], check=True)
                    logger.info("Changes unstaged. Aborting commit.")
                    raise typer.Exit(1)
        except subprocess.CalledProcessError:
            # This might happen in detached HEAD state
            logger.error(
                "Error checking for merge conflicts - you may be in detached HEAD state"
            )
            logger.error("Aborting to be safe")
            subprocess.run(["git", "reset"], check=True)
            raise typer.Exit(1)
    logger.info("No merge conflicts found with parent branch")


def git_save(
    message: str | None,
    no_verify: bool,
    no_sync: bool,
    amend: bool,
    pathspec: list[str] | None,
) -> None:
    if not message and not amend:
        raise typer.BadParameter("Commit message or --amend is required")
    current_branch = get_current_branch_name()
    default_branch = get_default_branch()

    # Check if the current branch is a default branch
    current_org = os.getenv("CURRENT_ORG")
    if current_org:
        remote_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # Extract org from GitHub URL (handles both HTTPS and SSH formats)
        org_match = re.search(r"[:/]([^/]+)/[^/]+$", remote_url)
        if (
            org_match
            and org_match.group(1) == current_org.replace("_", "-")
            and current_branch == default_branch
        ):
            assert message, "Message is required when committing to a default branch"
            new_branch_name = "devon/" + message.replace(" ", "_")
            should_commit = input(
                "On a default branch. "
                + f"Commit to a new branch called {new_branch_name}? (y/n): "
            )
            if should_commit.lower() == "y":
                subprocess.run(
                    ["git-town", "append", new_branch_name],
                    check=True,
                )
            else:
                logger.info(
                    "Changes not committed. Use `git commit` to commit to a default branch."
                )
                raise typer.Exit(1)
        else:
            logger.info(
                "Not on the default branch. "
                + f"Continuing from this branch: {current_branch}"
            )

    # Add changes to the staging area
    git_add_command = ["git", "add"]
    if pathspec:
        git_add_command.extend(pathspec)
    else:
        git_add_command.append("-A")
    subprocess.run(git_add_command, check=True)

    # Check for conflicts with the parent branch
    check_for_parent_branch_merge_conflicts(
        current_branch=current_branch, default_branch=default_branch
    )

    # Commit the changes
    git_commit_command = ["git", "commit"]
    if message:
        git_commit_command.append("-m")
        git_commit_command.append(message)
    if amend:
        git_commit_command.append("--amend")
    if no_verify:
        git_commit_command.append("--no-verify")
    subprocess.run(
        git_commit_command,
        check=True,
        text=True,
    )

    # Sync the changes
    if not no_sync:
        sync_repo()

    if message:
        store_commit(message, current_repo_name(), current_repo_org(), current_branch)

    # Print the status
    logger.info("git status:")
    subprocess.run(["git", "status"], check=True)


def get_branch_name(
    branch: str | None, command: Literal["change", "combine"] | None = None
) -> str:
    if not branch:
        # Interactive branch selection
        branches: str = subprocess.run(
            ["git", "branch"], check=True, capture_output=True, text=True
        ).stdout
        fzf = subprocess.Popen(
            ["fzf"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        selected_branch, _ = fzf.communicate(input=branches)
        branch_name = selected_branch.strip()
    elif branch in DEFAULT_BRANCH_NAMES:
        branch_name = get_default_branch()
    elif branch == "-":
        if command == "combine":
            branch_name = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "@{-1}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        else:
            branch_name = branch
    elif command == "change":
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                check=True,
                capture_output=True,
                text=True,
            )
            branch_name = branch
        except subprocess.CalledProcessError:
            should_create_branch = input(
                f"Branch '{branch}' does not exist. Would you like to create it? (y/n): "
            )
            if should_create_branch.lower() == "y":
                subprocess.run(
                    ["git-town", "append", branch],
                    check=True,
                )
                branch_name = branch
            else:
                logger.error("Exiting. Unable to continue without a valid branch name.")
                raise typer.Exit(1)
    else:
        branch_name = branch
    return branch_name


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


def git_setup(
    target_path: Path,
    git_projects_workdir: Path,
    service_name: str | None = None,
    index_serena: bool = True,
) -> None:
    os.chdir(target_path)
    # Create .gitignored symlink to technical documentation directory
    techdocs_source = git_projects_workdir / "tech-docs" / f"{target_path.name}"
    techdocs_target = target_path / ".techdocs"
    if techdocs_source.exists() and not techdocs_target.exists():
        os.symlink(techdocs_source, techdocs_target)
    # Create .gitignored file for setup script. Ran for worktrees setup.
    setup_script = target_path / ".setup.sh"
    if not setup_script.exists():
        setup_script.write_text("#!/usr/bin/env sh\n")
        setup_script.chmod(0o755)
    if not (target_path / ".git-branches.toml").exists():
        git_branches_path = git_projects_workdir / "dotfiles/config/.git-branches.toml"
        git_branches = toml.loads(git_branches_path.read_text())
        default_branch = get_default_branch()
        git_branches["branches"]["main"] = default_branch
        (target_path / ".git-branches.toml").write_text(toml.dumps(git_branches))
    env_vars = render_helm_yaml(
        target_path,
        git_projects_workdir,
        service_name,
    )
    if (target_path / ".env").exists():
        original_dot_env = (target_path / ".env").read_text() if env_vars else ""
    else:
        (target_path / ".env").touch()
        original_dot_env = ""
    if env_vars:
        original_env_var_lines = [
            line.strip() for line in original_dot_env.split("\n") if line.strip()
        ]
        env_var_lines = [
            f"{k}={v}"
            for k, v in env_vars.items()
            if f"{k}={v}" not in original_env_var_lines
        ]
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updated_dot_env = (
            f"# ~~~ Start of env vars generated by toolbelt on {generated_at} ~~~\n"
            + "\n".join(env_var_lines)
            + f"\n# ~~~ End of env vars generated by toolbelt on {generated_at} ~~~\n"
            + original_dot_env
        )
        (target_path / ".env").write_text(updated_dot_env)
    if not (target_path / ".envrc").exists():
        (target_path / ".envrc").touch()
    if "dotenv" not in (target_path / ".envrc").read_text():
        with open(target_path / ".envrc", "a") as f:
            f.write("dotenv\n")
    if (target_path / ".pre-commit-config.yaml").exists():
        subprocess.run(["pre-commit", "install"], check=True)
    # Create .cursor/rules symlink to cursor rules directory
    cursor_rules_source = git_projects_workdir / "dotfiles/cursor/rules"
    cursor_rules_target = target_path / ".cursor/rules"
    if cursor_rules_source.exists() and not cursor_rules_target.exists():
        os.symlink(cursor_rules_source, cursor_rules_target)
    if not (target_path / ".serena/project.yml").exists():
        subprocess.run(
            [
                "uvx",
                "--from",
                "git+https://github.com/oraios/serena",
                "serena",
                "project",
                "create",
            ],
            check=True,
            cwd=target_path,
        )
    if index_serena:
        subprocess.run(
            [
                "uvx",
                "--from",
                "git+https://github.com/oraios/serena",
                "serena",
                "project",
                "index",
            ],
            check=True,
            cwd=target_path,
        )
    update_repo(target_path)


def get_aws_secret(secret_name: str, region: str = "us-east-1") -> dict:
    session = boto3.session.Session()
    client = session.client(service_name="secretsmanager", region_name=region)

    response = client.get_secret_value(SecretId=secret_name)
    if "SecretString" in response:
        return json.loads(response["SecretString"])
    else:
        raise RuntimeError(f"No secret string found for {secret_name}")


def escape_env_value(value: str) -> str:
    """
    Escape environment variable values that contain special characters.
    Returns the original value if no escaping is needed.
    """
    # Check if value needs escaping
    needs_escaping = any(
        (
            "\n" in value,  # newlines
            " " in value,  # spaces
            "<" in value,  # angle brackets
            ">" in value,
            '"' in value,  # quotes
            "$" in value,  # shell variables
            "#" in value,  # comments
            ";" in value,  # command separators
            "&" in value,
            "|" in value,
            "(" in value,  # parentheses
            ")" in value,
            "[" in value,  # brackets
            "]" in value,
            "{" in value,  # braces
            "}" in value,
        )
    )

    if not needs_escaping:
        return value

    # Escape shell variables first
    escaped = value.replace("${", "\\${")
    # Escape any existing quotes
    escaped = escaped.replace('"', '\\"')
    # Wrap in quotes to preserve special characters
    return f'"{escaped}"'


def render_helm_yaml(
    repo_path: Path,
    git_projects_workdir: Path,
    service_name: str | None = None,
) -> dict:
    repo_name = Path.cwd().name if str(repo_path) == "." else repo_path.name
    charts_path = Path(
        git_projects_workdir,
        repo_name,
        "charts",
        service_name or repo_name,
    )
    if not charts_path.exists():
        return {}
    helm_params = []
    service_default_values = charts_path / "values.yaml"
    if service_default_values.exists():
        helm_params.append("-f")
        helm_params.append(str(service_default_values))
    standard_values = (
        git_projects_workdir / "helm-releases/releases/dbt-labs/devspace/main.yaml"
    )
    if standard_values.exists():
        helm_params.append("-f")
        helm_params.append(str(standard_values))
    service_devspace_values = (
        git_projects_workdir
        / f"helm-releases/releases/dbt-labs/devspace/{service_name or repo_name}.yaml"
    )
    if service_devspace_values.exists():
        helm_params.append("-f")
        helm_params.append(str(service_devspace_values))
    if not helm_params:
        return {}
    process = subprocess.run(
        [
            "helm",
            "template",
            "dev",
            str(charts_path),
        ]
        + helm_params,
        capture_output=True,
        text=True,
        check=True,
    )
    if process.stderr:
        raise RuntimeError(f"Error running helm command: {process.stderr}")
    rendered_yaml = process.stdout
    documents = yaml.safe_load_all(rendered_yaml)
    dot_envs = {}
    for doc in documents:
        if isinstance(doc, dict):
            kind = doc.get("kind")
            if kind == "ConfigMap":
                config_data = doc.get("data", {})
                dot_envs.update(config_data)
            elif kind == "ExternalSecret":
                spec = doc.get("spec", {})
                if spec.get("backendType") == "secretsManager":
                    data_from = spec.get("dataFrom", [])
                    for secret_ref in data_from:
                        if isinstance(secret_ref, str):
                            secrets = get_aws_secret(secret_ref)
                            dot_envs.update(secrets)

    # direnv can't handle env vars with newlines even with escaping.
    return {k: escape_env_value(v) for k, v in dot_envs.items() if "\n" not in v}


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(path),
        )
        return True
    except subprocess.CalledProcessError:
        return False


def git_safe_pull() -> None:
    """
    Safely pull changes from remote by checking for uncommitted changes and
    ensuring the pull can be done without conflicts.
    """
    # Check for uncommitted changes first
    uncommitted_check = subprocess.run(
        ["git", "diff-index", "--quiet", "HEAD", "--"],
        capture_output=True,
        text=True,
        check=False,
    )
    if uncommitted_check.returncode != 0:
        logger.warning(
            "⚠️  Uncommitted changes found. Please commit or stash them before pulling."
        )
        raise typer.Exit(1)

    current_branch = get_current_branch_name()

    # Fetch latest changes
    logger.info("Fetching latest changes...")
    subprocess.run(["git", "fetch"], check=True)

    # Check if current branch has diverged from remote
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{current_branch}"],
            check=True,
            capture_output=True,
            text=True,
        )

        # If we get here, it's safe to pull
        logger.info("Branch can be fast-forwarded. Pulling changes...")
        subprocess.run(["git", "pull"], check=True)
        logger.info("Successfully pulled changes!")

    except subprocess.CalledProcessError:
        logger.warning(
            "⚠️  Warning: Local branch has diverged from remote. Pulling might cause conflicts."
        )
        raise typer.Exit(1)
