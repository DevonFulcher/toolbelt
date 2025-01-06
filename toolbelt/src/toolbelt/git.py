import os
from pathlib import Path
import re
import sys
import subprocess
from typing import Literal
import toml
import yaml
from toolbelt.repos import current_repo
import argparse
from toolbelt.env_var import get_env_var_or_exit, get_git_projects_workdir
import boto3
import json


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


def git_pr():
    view_pr = subprocess.run(["gh", "pr", "view", "--web"])
    if view_pr.returncode == 0:
        return
    repo = current_repo()
    if repo:
        repo.unit()
    subprocess.run(
        ["git-town", "propose"],
    )


def git_save(args: argparse.Namespace) -> None:
    current_org = os.getenv("CURRENT_ORG")
    if current_org:
        current_branch = get_current_branch_name()
        default_branch = get_default_branch()
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
            print(
                "Cannot commit to the default branch"
                + " for a repo of the current organization.",
                file=sys.stderr,
            )
            sys.exit(1)

    git_add_command = ["git", "add"]
    if args.pathspec:
        git_add_command.extend(args.pathspec)
    else:
        git_add_command.append("-A")
    subprocess.run(git_add_command, check=True)

    git_commit_command = [
        "git",
        "commit",
        "-m",
        args.message,
    ]
    if args.no_verify:
        git_commit_command.append("--no-verify")
    commit_result = subprocess.run(
        git_commit_command,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode == 0:
        print("code committed")
    else:
        print(commit_result.stderr, file=sys.stderr)
        sys.exit(1)

    if not args.no_push:
        git_push_command = ["git", "push"]
        if args.force:
            git_push_command.append("-f")
        push_result = subprocess.run(git_push_command, capture_output=True)
        if push_result.returncode == 0:
            print("commit pushed")
        else:
            print(str(push_result.stderr), file=sys.stderr)
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
    elif args.branch == "-" and args.command == "combine":
        branch_name = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "@{-1}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    else:
        branch_name = args.branch
    if not branch_name:
        print("No branch name provided.", file=sys.stderr)
        sys.exit(1)
    return branch_name


def git_setup(target_path: Path, git_projects_workdir: Path) -> None:
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


def render_helm_yaml(repo_path: Path, git_projects_workdir: Path) -> dict:
    service_name = Path.cwd().name if str(repo_path) == "." else repo_path.name
    charts_path = Path(
        git_projects_workdir,
        service_name,
        "charts",
        service_name,
    )
    if not charts_path.exists():
        return {}
    helm_params = []
    service_default_values = charts_path / "values.yaml"
    if service_default_values.exists():
        helm_params.append("-f")
        helm_params.append(str(service_default_values))
    standard_values = (
        git_projects_workdir / "helm-releases/releases/dbt-labs/dev/main.yaml"
    )
    if standard_values.exists():
        helm_params.append("-f")
        helm_params.append(str(standard_values))
    service_dev_values = (
        git_projects_workdir
        / f"helm-releases/releases/dbt-labs/dev/{service_name}.yaml"
    )
    if service_dev_values.exists():
        helm_params.append("-f")
        helm_params.append(str(service_dev_values))
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

    return dot_envs


def git(args: argparse.Namespace):
    git_projects_workdir = get_git_projects_workdir()
    match args.git_command:
        case "pr":
            git_pr()
        case "get":
            repo_name = re.sub(
                r"\..*$",
                "",
                os.path.basename(args.repo_url),
            )
            clone_path = git_projects_workdir / repo_name
            subprocess.run(
                ["git", "clone", args.repo_url, str(clone_path)],
                check=True,
            )
            if not os.path.isdir(clone_path):
                raise ValueError(f"Failed to clone {args.repo_url} to {clone_path}")
            git_setup(
                target_path=clone_path,
                git_projects_workdir=git_projects_workdir,
            )
            # TODO: This can use the `edit` shell function
            editor = get_env_var_or_exit("EDITOR")
            subprocess.run([editor, str(clone_path)], check=True)
        case "setup":
            git_setup(
                target_path=Path(args.repo_path),
                git_projects_workdir=git_projects_workdir,
            )
        case "save":
            git_save(args)
        case "send":
            current_branch = get_current_branch_name()
            default_branch = get_default_branch()
            if default_branch == current_branch:
                new_branch_name = args.message.replace(" ", "_")
                print(
                    "On a default branch. "
                    + f"Creating a new branch called {new_branch_name}"
                )
                subprocess.run(
                    ["git-town", "append", new_branch_name],
                    check=True,
                )
            else:
                print(
                    "Not on a default branch. "
                    + f"Continuing from this branch: {current_branch}"
                )
            git_save(args)
            git_pr()
        case "change":
            subprocess.run(["git", "checkout", get_branch_name(args)], check=True)
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
        case _:
            print(f"Unknown command: {args.command}")
            sys.exit(1)