import argparse
import subprocess

from toolbelt.datadog_form import form as datadog_form
from toolbelt.git import git
from toolbelt.repos import current_repo


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Git workflow helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Upgrade command
    subparsers.add_parser("upgrade", help="Upgrade toolbelt")

    # Unit command
    subparsers.add_parser("unit", help="Run unit tests for this repo")

    # Datadog command
    subparsers.add_parser("datadog", aliases=["dd"], help="Datadog form")

    # Git command and its subcommands
    git_parser = subparsers.add_parser("git", help="Git workflow commands")
    git_subparsers = git_parser.add_subparsers(dest="git_command", required=True)

    # PR command
    git_pr_parser = git_subparsers.add_parser(
        "pr",
        help="Create or view a pull request",
    )
    git_pr_parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip tests",
    )

    # Get command
    get_parser = git_subparsers.add_parser("get", help="Get a repository")
    get_parser.add_argument("repo_url", help="URL of the repository to get")
    get_parser.add_argument(
        "--service-name",
        help="The name of the service for retrieving helm values. If not provided, the repo name will be used.",
    )

    # Save command
    save_parser = git_subparsers.add_parser("save", help="Save and push changes")
    save_parser.add_argument("-m", "--message", required=True, help="Commit message")
    save_parser.add_argument(
        "--no-verify", action="store_true", help="Skip pre-commit hooks"
    )
    save_parser.add_argument(
        "--no-sync", action="store_true", help="Skip syncing changes in this stack"
    )
    save_parser.add_argument(
        "pathspec", nargs="*", help="Files to stage (defaults to '-A')"
    )

    # Send command
    send_parser = git_subparsers.add_parser("send", help="Save changes and create PR")
    send_parser.add_argument(
        "-m", "--message", required=True, help="Commit/branch message"
    )
    send_parser.add_argument(
        "--no-verify", action="store_true", help="Skip pre-commit hooks"
    )
    send_parser.add_argument("-f", "--force", action="store_true", help="Force push")
    send_parser.add_argument(
        "pathspec", nargs="*", help="Files to stage (defaults to '-A')"
    )
    send_parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip tests",
    )
    send_parser.add_argument(
        "--no-sync", action="store_true", help="Skip syncing changes in this stack"
    )

    # Change command
    change_parser = git_subparsers.add_parser("change", help="Change git branch")
    change_parser.add_argument(
        "branch",
        nargs="?",
        help="Branch name to change to. If omitted, will use fzf to select",
    )
    change_parser.add_argument(
        "-b",
        help="Create a new branch and switch to it",
    )

    # Compare command
    compare_parser = git_subparsers.add_parser(
        "compare", help="Compare commits with git diff"
    )
    compare_parser.add_argument(
        "compare_args", nargs="*", help="Commands to pass to git diff"
    )

    # Combine command
    combine_parser = git_subparsers.add_parser("combine", help="Combine branches")
    combine_parser.add_argument("branch", help="Branch to combine")

    # Setup command
    setup_parser = git_subparsers.add_parser("setup", help="Setup git config")
    setup_parser.add_argument("repo_path", help="Path to the repository to setup")
    setup_parser.add_argument(
        "--service-name",
        help="The name of the service for retrieving helm values. If not provided, the repo name will be used.",
    )

    # Safe pull command
    safe_pull_parser = git_subparsers.add_parser("safe-pull", help="Safe pull")

    # Replace command
    replace_parser = git_subparsers.add_parser("replace", help="Replace")
    replace_parser.add_argument(
        "-m",
        "--message",
        required=False,
        help="Commit message to replace original commit message",
    )

    return parser


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()
    match args.command:
        case "upgrade":
            subprocess.run(
                [
                    "uv",
                    "tool",
                    "upgrade",
                    "--reinstall",
                    "toolbelt",
                ],
                check=True,
            )
        case "datadog":
            datadog_form()
        case "git":
            git(args)
        case "unit":
            repo = current_repo()
            if repo:
                repo.unit()
            else:
                print("No unit tests configured for this repo")


if __name__ == "__main__":
    main()
