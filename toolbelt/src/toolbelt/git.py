import os
from pathlib import Path
import re
import sys
import subprocess
from typing import Literal
import toml
from toolbelt.repos import current_repo
import argparse
from toolbelt.env_var import get_env_var_or_exit, get_git_projects_workdir


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
    if args.branch == "-" and args.command == "combine":
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


def git(args: argparse.Namespace):
    git_projects_workdir = get_git_projects_workdir()
    match args.git_command:
        case "pr":
            git_pr()
        case "get":
            repo_name = re.sub(r"\..*$", "", os.path.basename(args.repo_url))
            clone_path = os.path.join(git_projects_workdir, repo_name)
            subprocess.run(["git", "clone", args.repo_url, clone_path], check=True)
            if os.path.isdir(clone_path):
                git_branches_path = (
                    git_projects_workdir / "dotfiles/config/.git-branches.toml"
                )
                with git_branches_path.open("r") as f:
                    git_branches = toml.loads(f.read())
                os.chdir(clone_path)
                default_branch = get_default_branch()
                git_branches["branches"]["main"] = default_branch
                Path(clone_path, ".git-branches.toml").write_text(
                    toml.dumps(git_branches)
                )
                Path(clone_path, ".env").touch()
                Path(clone_path, ".envrc").write_text("dotenv\n")
                editor = get_env_var_or_exit("EDITOR")
                subprocess.run([editor, "."], check=True)
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
                ]
            )
        case _:
            print(f"Unknown command: {args.command}")
            sys.exit(1)
