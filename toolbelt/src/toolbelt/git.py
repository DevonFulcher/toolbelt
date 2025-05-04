import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

import boto3
import toml
import yaml

from toolbelt.env_var import get_env_var_or_exit, get_git_projects_workdir
from toolbelt.repos import current_repo


def get_default_branch() -> Literal["main", "master"]:
    try:
        # Check if 'main' branch exists
        subprocess.run(
            ["git", "rev-parse", "--verify", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
        return "main"
    except subprocess.CalledProcessError:
        try:
            # Check if 'master' branch exists
            subprocess.run(
                ["git", "rev-parse", "--verify", "master"],
                check=True,
                capture_output=True,
                text=True,
            )
            return "master"
        except subprocess.CalledProcessError:
            print("Neither 'main' nor 'master' branch exists.", file=sys.stderr)
            sys.exit(1)


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


def git_pr(args: argparse.Namespace):
    view_pr = subprocess.run(["gh", "pr", "view", "--web"])
    if view_pr.returncode == 0:
        return
    repo = current_repo()
    if repo and not args.skip_tests:
        repo.unit()
    subprocess.run(
        ["gh", "pr", "create", "--web"],
    )

def check_for_parent_branch_merge_conflicts(*, current_branch: str, default_branch: str) -> None:
    print("Checking for merge conflicts with parent branch")
    try:
        parent_branch = get_parent_branch_name(current_branch)
    except subprocess.CalledProcessError:
        if current_branch == default_branch:
            # Default branch without upstream - this is unusual but can happen
            print(f"Warning: {default_branch} has no upstream branch set")
            sys.exit(1)
        else:
            # Regular branch without upstream - offer to set it
            print(f"Branch '{current_branch}' has no upstream branch set")
            should_set_upstream = input("Would you like to set an upstream branch? (y/n): ")
            if should_set_upstream.lower() == 'y':
                try:
                    # Try to set upstream to origin/branch_name
                    subprocess.run(
                        ["git", "branch", "--set-upstream-to", f"origin/{current_branch}", current_branch],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    print(f"Set upstream branch to origin/{current_branch}")
                    # Try conflict check again with newly set upstream
                    parent_branch = get_parent_branch_name(current_branch)
                except subprocess.CalledProcessError:
                    # If setting upstream failed (e.g., remote branch doesn't exist)
                    print("Failed to set upstream branch - remote branch may not exist")
                    should_push = input("Would you like to push and set upstream now? (y/n): ")
                    if should_push.lower() == 'y':
                        try:
                            subprocess.run(
                                ["git", "push", "--set-upstream", "origin", current_branch],
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                            print(f"Pushed and set upstream to origin/{current_branch}")
                            # Try conflict check one final time
                            parent_branch = get_parent_branch_name(current_branch)
                        except subprocess.CalledProcessError:
                            print("Failed to push and set upstream branch")
                            sys.exit(1)
                    else:
                        sys.exit(1)
            else:
                sys.exit(1)

    if parent_branch:
        try:
            merge_tree_result = subprocess.run(
                ["git", "merge-tree", parent_branch, current_branch],
                capture_output=True,
                text=True,
            )
            if "changed in both" in merge_tree_result.stdout:
                print("⚠️  Warning: This commit may create merge conflicts with the parent branch.")
                proceed = input("Do you want to continue anyway? (y/n): ")
                if proceed.lower() != 'y':
                    # Unstage changes if user aborts
                    subprocess.run(["git", "reset"], check=True)
                    print("Changes unstaged. Aborting commit.")
                    sys.exit(1)
        except subprocess.CalledProcessError as e:
            # This might happen in detached HEAD state
            print("Error checking for merge conflicts - you may be in detached HEAD state")
            print("Aborting to be safe")
            subprocess.run(["git", "reset"], check=True)
            sys.exit(1)
    print("No merge conflicts found with parent branch")


def git_save(args: argparse.Namespace) -> None:
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
            new_branch_name = args.message.replace(" ", "_")
            should_commit = input(
                "On a default branch. "
                + f"Should these changes be committed to a new branch called {new_branch_name}? (y/n): "
            )
            if should_commit.lower() == "y":
                subprocess.run(
                    ["git-town", "append", new_branch_name],
                    check=True,
                )
            else:
                print("Changes not committed. Use `git commit` to commit to a default branch.")
                sys.exit(1)
        else:
            print(
                "Not on the default branch. "
                + f"Continuing from this branch: {current_branch}"
            )


    # Add changes to the staging area
    git_add_command = ["git", "add"]
    if args.pathspec:
        git_add_command.extend(args.pathspec)
    else:
        git_add_command.append("-A")
    subprocess.run(git_add_command, check=True)

    # Check for conflicts with the parent branch
    check_for_parent_branch_merge_conflicts(current_branch=current_branch, default_branch=default_branch)

    # Commit the changes
    git_commit_command = [
        "git",
        "commit",
        "-m",
        args.message,
    ]
    if args.no_verify:
        git_commit_command.append("--no-verify")
    subprocess.run(
        git_commit_command,
        check=True,
        text=True,
    )

    # Sync the changes
    if not hasattr(args, "no_sync") or not args.no_sync:
        # Run git-town continue in case a git conflict happened during the last save
        subprocess.run(["git-town", "continue"], check=True)
        subprocess.run(["git-town", "sync", "--stack"], check=True)

    # Print the status
    print("git status:")
    subprocess.run(["git", "status"], check=True)


def get_branch_name(args: argparse.Namespace) -> str:
    if not args.branch:
        # Interactive branch selection
        branches: str = subprocess.run(
            ["git", "branch"], check=True, capture_output=True, text=True
        ).stdout
        fzf = subprocess.Popen(
            ["fzf"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        selected_branch, _ = fzf.communicate(input=branches)
        branch_name = selected_branch.strip()
    elif args.branch in ["main", "master"]:
        branch_name = get_default_branch()
    elif args.branch == "-":
        if args.command == "combine":
            branch_name = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "@{-1}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        else:
            branch_name = args.branch
    else:
        if args.command == "change":
            try:
                subprocess.run(
                    ["git", "rev-parse", "--verify", args.branch],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                branch_name = args.branch
            except subprocess.CalledProcessError:
                should_create_branch = input(f"Branch '{args.branch}' does not exist. Would you like to create it? (y/n): ")
                if should_create_branch.lower() == "y":
                    subprocess.run(
                        ["git-town", "append", args.branch],
                        check=True,
                    )
                    branch_name = args.branch
                else:
                    print("Exiting. Unable to continue without a valid branch name.", file=sys.stderr)
                    sys.exit(1)
        else:
            branch_name = args.branch
    return branch_name


def git_setup(
    target_path: Path,
    git_projects_workdir: Path,
    service_name: str | None = None,
) -> None:
    os.chdir(target_path)
    if not (target_path / ".git-branches.toml").exists():
        git_branches_path = git_projects_workdir / "dotfiles/config/.git-branches.toml"
        git_branches = toml.loads(git_branches_path.read_text())
        default_branch = get_default_branch()
        git_branches["branches"]["main"] = default_branch
        Path(target_path, ".git-branches.toml").write_text(toml.dumps(git_branches))
    env_vars = render_helm_yaml(
        target_path,
        git_projects_workdir,
        service_name,
    )
    if (target_path / ".env").exists():
        if env_vars:
            original_dot_env = (target_path / ".env").read_text()
        else:
            original_dot_env = ""
    else:
        Path(target_path, ".env").touch()
        original_dot_env = ""
    if env_vars:
        updated_dot_env = (
            "# ~~~ Start of env vars generated by toolbelt ~~~\n"
            + "\n".join([f"{k}={v}" for k, v in env_vars.items()])
            + "\n# ~~~ End of env vars generated by toolbelt ~~~\n"
            + original_dot_env
        )
        (target_path / ".env").write_text(updated_dot_env)
    if not (target_path / ".envrc").exists():
        Path(target_path, ".envrc").touch()
    if "dotenv" not in (target_path / ".envrc").read_text():
        with open(target_path / ".envrc", "a") as f:
            f.write("dotenv\n")


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
    needs_escaping = any((
        '\n' in value,         # newlines
        ' ' in value,          # spaces
        '<' in value,          # angle brackets
        '>' in value,
        '"' in value,          # quotes
        '$' in value,          # shell variables
        '#' in value,          # comments
        ';' in value,          # command separators
        '&' in value,
        '|' in value,
        '(' in value,          # parentheses
        ')' in value,
        '[' in value,          # brackets
        ']' in value,
        '{' in value,          # braces
        '}' in value,
    ))

    if not needs_escaping:
        return value

    # Escape shell variables first
    escaped = value.replace('${', '\\${')
    # Escape any existing quotes
    escaped = escaped.replace('"', '\\"')
    # Wrap in quotes to preserve special characters
    return f'"{escaped}"'

def render_helm_yaml(
    repo_path: Path,
    git_projects_workdir: Path,
    service_name: str | None = None,
) -> dict:
    repo_name = (
        Path.cwd().name
        if str(repo_path) == "."
        else repo_path.name
    )
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
    )
    if uncommitted_check.returncode != 0:
        print("⚠️  Uncommitted changes found. Please commit or stash them before pulling.")
        sys.exit(1)

    current_branch = get_current_branch_name()

    # Fetch latest changes
    print("Fetching latest changes...")
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
        print("Branch can be fast-forwarded. Pulling changes...")
        subprocess.run(["git", "pull"], check=True)
        print("Successfully pulled changes!")

    except subprocess.CalledProcessError:
        print("⚠️  Warning: Local branch has diverged from remote. Pulling might cause conflicts.")
        sys.exit(1)


def git_replace(args: argparse.Namespace) -> None:
    """
    Squashes the current changes into the last commit.
    If a message is provided, it will be used as the new commit message.
    """
    # If no message provided, get the original commit message before resetting
    if not args.message:
        args.message = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    # Reset the last commit while keeping the changes
    subprocess.run(["git", "reset", "--soft", "HEAD~1"], check=True)

    # Use git_save to handle adding changes, committing, and pushing
    git_save(args)

def git(args: argparse.Namespace):
    git_projects_workdir = get_git_projects_workdir()
    match args.git_command:
        case "pr":
            git_pr(args)
        case "get":
            repo_name = re.sub(
                r"\..*$",
                "",
                os.path.basename(args.repo_url),
            )
            clone_path = git_projects_workdir / repo_name
            if not is_git_repo(clone_path):
                subprocess.run(
                    ["git", "clone", args.repo_url, str(clone_path)],
                    check=True,
                )
                if not os.path.isdir(clone_path):
                    raise ValueError(f"Failed to clone {args.repo_url} to {clone_path}")
            git_setup(
                target_path=clone_path,
                git_projects_workdir=git_projects_workdir,
                service_name=args.service_name,
            )
            # TODO: This can use the `edit` shell function
            editor = get_env_var_or_exit("EDITOR")
            subprocess.run([editor, str(clone_path)], check=True)
        case "setup":
            git_setup(
                target_path=Path(args.repo_path),
                git_projects_workdir=git_projects_workdir,
                service_name=args.service_name,
            )
        case "save":
            git_save(args)
        case "send":
            git_save(args)
            git_pr(args)
        case "change":
            if args.new_branch:
                subprocess.run(["git", "checkout", "-b", args.new_branch], check=True)
            else:
                subprocess.run(["git", "checkout", get_branch_name(args)], check=True)
                git_safe_pull()
        case "combine":
            subprocess.run(["git", "merge", get_branch_name(args)], check=True)
        case "compare":
            # Exclude files from diff that I rarely care about. Reference: https://stackoverflow.com/a/48259275/8925314
            subprocess.run(
                ["git", "diff"]
                + args.compare_args
                + [
                    "--",
                    ":!*Cargo.lock",
                    ":!*poetry.lock",
                    ":!*package-lock.json",
                    ":!*pnpm-lock.yaml",
                    ":!*uv.lock",
                    ":!*go.sum",
                ]
            )
        case "safe-pull":
            git_safe_pull()
        case "replace":
            git_replace(args)
        case _:
            print(f"Unknown command: {args.command}")
            sys.exit(1)
