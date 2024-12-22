import argparse

from toolbelt.git import git
from toolbelt.repos import current_repo


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Git workflow helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("unit", help="Run unit tests for this repo")

    # Git command and its subcommands
    git_parser = subparsers.add_parser("git", help="Git workflow commands")
    git_subparsers = git_parser.add_subparsers(dest="git_command", required=True)

    # PR command
    git_subparsers.add_parser("pr", help="Create or view a pull request")

    # Get command
    get_parser = git_subparsers.add_parser("get", help="Get a repository")
    get_parser.add_argument("repo_url", help="URL of the repository to get")

    # Save command
    save_parser = git_subparsers.add_parser("save", help="Save and push changes")
    save_parser.add_argument("-m", "--message", required=True, help="Commit message")
    save_parser.add_argument(
        "--no-verify", action="store_true", help="Skip pre-commit hooks"
    )
    save_parser.add_argument("-f", "--force", action="store_true", help="Force push")
    save_parser.add_argument(
        "--no-push", action="store_true", help="Skip pushing changes"
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
        "--no-push", action="store_true", help="Skip pushing changes"
    )
    send_parser.add_argument(
        "pathspec", nargs="*", help="Files to stage (defaults to '-A')"
    )

    # Change command
    change_parser = git_subparsers.add_parser("change", help="Change git branch")
    change_parser.add_argument(
        "branch",
        nargs="?",
        help="Branch name to change to. If omitted, will use fzf to select",
    )

    # Compare command
    compare_parser = git_subparsers.add_parser(
        "compare", help="Compare commits with git diff"
    )
    compare_parser.add_argument(
        "compare_args", nargs="*", help="Commands to pass to git diff"
    )

    return parser


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()
    match args.command:
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
