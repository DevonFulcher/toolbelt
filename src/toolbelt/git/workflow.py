import os
import re
import subprocess
from pathlib import Path

import typer

from toolbelt.git.commits import store_commit
from toolbelt.git.constants import GIT_BRANCH_PREFIX
from toolbelt.git.exec import run
from toolbelt.logger import logger
from toolbelt.repos import current_repo, current_repo_name

from .branches import (
    get_current_branch_name,
    get_default_branch,
)
from .repo import current_repo_org, get_current_repo_root_path
from .worktrees_ops import delete_branch_and_worktree, main_worktree


def update_repo(target_path: Path):
    if (target_path / ".tool-versions").exists():
        # This may fail if the plugins in .tool-versions are not installed
        subprocess.run(["asdf", "install"], check=True, cwd=target_path)
    if (target_path / "uv.lock").exists():
        subprocess.run(["uv", "sync", "--all-groups"], check=True, cwd=target_path)


def sync_repo(root: Path | None = None) -> None:
    """Merge-sync the whole stack the current branch belongs to and refresh deps.

    Replaces the old git-town flow with the homegrown stack sync: each branch is
    merged parent -> child inside its own worktree and pushed (so no extra
    ``git push`` here — ``sync_stack`` pushes every branch). ``root`` defaults to
    the current worktree; pass a specific worktree to sync a stack the caller
    just moved into (e.g. a freshly created branch).

    ``root`` itself can end up deleted by either step below: ``sync_stack``
    removes a landed branch's worktree (which can be ``root``, e.g. syncing
    from inside a branch whose own PR just merged), and so can
    ``git_branch_clean``. ``update_repo`` needs a real, still-existing path,
    so it falls back to the main worktree when ``root`` no longer exists.
    """
    # Imported lazily: worktrees -> bootstrap.repo_setup -> workflow would be a
    # circular import at module load time.
    from toolbelt.git.stack.forge import GhForge
    from toolbelt.git.stack.sync import sync_stack
    from toolbelt.git.worktrees import repo_root

    root = root or repo_root()
    main_wt = main_worktree(root)
    sync_stack(root=root, forge=GhForge(root))
    git_branch_clean(root)
    update_repo(root if root.exists() else main_wt)


def git_merge(pr: str, cwd: Path | None = None) -> None:
    """Squash-merge a PR, then sync the stack.

    ``pr`` is passed through to ``gh pr merge`` as-is (a PR number, URL, or
    branch name). ``cwd`` targets the branch's worktree, mirroring
    ``git_pr``. Syncing afterward restacks any children onto the branch's
    parent and cleans up the now-landed branch (see ``sync_stack``'s
    restack-on-land handling).
    """
    subprocess.run(["gh", "pr", "merge", "--squash", pr], check=True, cwd=cwd)
    sync_repo(cwd)


def git_pr(skip_tests: bool, cwd: Path | None = None) -> None:
    # ``cwd`` targets the branch's worktree: when `git save` starts a new stacked
    # branch it lands in its own worktree, so the PR must be opened from there
    # rather than the (default-branch) directory the command was invoked in.
    view_pr = subprocess.run(["gh", "pr", "view", "--web"], check=False, cwd=cwd)
    if view_pr.returncode == 0:
        return
    repo = current_repo()
    if repo and not skip_tests:
        repo.unit()
    # Stack-aware base: target the branch's recorded stack parent (see
    # ``toolbelt.git.stack.lineage``) rather than always the repo's default
    # branch, so a PR for a branch stacked on another open PR's branch is
    # opened against that branch instead of comparing the whole stack to main.
    # ``root`` must resolve against ``cwd`` (not the process's actual cwd,
    # which may differ — e.g. `git send` calls this with the freshly created
    # branch's worktree as ``cwd`` without chdir'ing the process into it).
    from toolbelt.git.stack.lineage import get_parent
    from toolbelt.git.worktrees import current_branch, repo_root

    root = cwd or repo_root()
    branch = current_branch(root)
    base = get_parent(branch, root=root) or get_default_branch()
    subprocess.run(
        ["gh", "pr", "create", "--web", "--base", base],
        check=False,
        cwd=cwd,
    )


def _drop_lineage_entry(branch: str, *, root: Path) -> None:
    """Drop ``branch``'s own lineage entry, reparenting any tracked children
    onto its former parent first (its own history is gone, so a later
    ``sync`` of that child's stack will merge/rebase it onto the
    grandparent)."""
    # Lazy import: worktrees -> bootstrap.repo_setup -> workflow would be a
    # circular import at module load time.
    from toolbelt.git.stack import lineage

    parent = lineage.get_parent(branch, root=root)
    if parent is not None:
        for child, child_parent in lineage.all_parents(root=root).items():
            if child_parent == branch:
                lineage.set_parent(child, parent, root=root)
    lineage.remove_parent(branch, root=root)


def _branch_exists(branch: str, *, root: Path) -> bool:
    return (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root,
        ).returncode
        == 0
    )


def git_branch_clean(root: Path | None = None) -> None:
    """
    Delete local branches whose upstream has been removed, and drop stale
    stack lineage entries — both this command's own deletions, and any
    tracked branch that's already gone by some other means (e.g. deleted
    directly with ``git branch -D``, bypassing this tool entirely). Without
    this, a gone branch keeps showing up as a ghost node in ``git tree``
    forever, since that just reads whatever lineage entries exist without
    checking the branch is still real.

    ``root`` defaults to the current worktree; pass one explicitly when
    calling from a context (e.g. ``sync_repo``) where the process's cwd may
    not be the worktree being cleaned. Either way, every git operation here
    actually runs from the repo's *main* worktree (see ``main_worktree``), not
    ``root`` itself — one of the "gone" branches can easily be the one
    checked out in ``root``, e.g. running this from inside the very worktree
    whose branch just got merged.
    """
    # Lazy import: worktrees -> bootstrap.repo_setup -> workflow would be a
    # circular import at module load time.
    from toolbelt.git.stack import lineage

    root = main_worktree(root or get_current_repo_root_path())
    subprocess.run(["git", "fetch", "-p"], check=True, cwd=root)
    branch_list = subprocess.run(
        ["git", "branch", "-vv"],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    ).stdout.splitlines()

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

        deleted_branch = delete_branch_and_worktree(branch_name, repo_root=root)
        _drop_lineage_entry(deleted_branch, root=root)
        deleted_branches.append(deleted_branch)

    stale_entries: list[str] = []
    for branch in lineage.all_parents(root=root):
        if branch in deleted_branches or _branch_exists(branch, root=root):
            continue
        _drop_lineage_entry(branch, root=root)
        stale_entries.append(branch)

    if deleted_branches:
        logger.info("Deleted branches:")
        for branch_name in deleted_branches:
            logger.info(f"  {branch_name}")
    else:
        logger.info("No branches to delete.")
    if stale_entries:
        logger.info("Dropped stack entries for already-gone branches:")
        for branch_name in stale_entries:
            logger.info(f"  {branch_name}")


def check_for_parent_branch_merge_conflicts(
    *, current_branch: str, root: Path, yes: bool
) -> None:
    """Warn (and optionally abort) if this commit may conflict with the branch's
    stack parent when synced.

    The parent comes from stack lineage (``toolbelt-stack.*``): a branch's
    upstream is its own remote, not its parent, so it can't be used here.
    Conflict detection uses ``git merge-tree --write-tree``, which performs a
    real merge and exits 1 on conflicts, 0 when clean, and >1 if it couldn't run.
    """
    # Lazy import to keep this module free of the stack-package import cycle.
    from toolbelt.git.stack.lineage import get_parent

    logger.info("Checking for merge conflicts with parent branch")
    parent_branch = get_parent(current_branch, root=root)
    if not parent_branch:
        logger.warning(
            f"Branch '{current_branch}' is not tracked in a stack; "
            "skipping parent-branch conflict check."
        )
        return

    merge_tree_result = subprocess.run(
        ["git", "merge-tree", "--write-tree", parent_branch, current_branch],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_tree_result.returncode > 1:
        # The check itself couldn't run (e.g. a missing ref); never block the
        # commit for a failed warning check — just skip it.
        logger.warning(
            f"Could not check for conflicts with parent '{parent_branch}' "
            f"({merge_tree_result.stderr.strip()}); skipping."
        )
        return
    if merge_tree_result.returncode == 0:
        return

    logger.warning(
        "⚠️  Warning: This commit may create merge conflicts with the parent "
        f"branch '{parent_branch}'."
    )
    if yes:
        logger.info("Continuing anyway due to --yes")
        return
    proceed = input("Do you want to continue anyway? (y/n): ")
    if proceed.lower() != "y":
        # Unstage changes if the user aborts.
        subprocess.run(["git", "reset"], check=True, cwd=root)
        logger.info("Changes unstaged. Aborting commit.")
        raise typer.Exit(1)


def _stage(*, root: Path, pathspec: list[str] | None) -> None:
    git_add_command = ["git", "add"]
    git_add_command.extend(pathspec if pathspec else ["-A"])
    subprocess.run(git_add_command, check=True, cwd=root)


def _commit(*, root: Path, message: str | None, amend: bool, no_verify: bool) -> None:
    git_commit_command = ["git", "commit"]
    if message:
        git_commit_command.extend(["-m", message])
    if amend:
        git_commit_command.append("--amend")
    if no_verify:
        git_commit_command.append("--no-verify")
    subprocess.run(git_commit_command, check=True, text=True, cwd=root)


def _should_start_new_branch(
    *, root: Path, current_branch: str, default_branch: str
) -> bool:
    """True when a save on the default branch should spin up a new stacked branch.

    Gated on CURRENT_ORG matching the repo's GitHub org, mirroring the previous
    behavior: only auto-branch in the user's own org's repos.
    """
    current_org = os.getenv("CURRENT_ORG")
    if not current_org or current_branch != default_branch:
        return False
    remote_url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    ).stdout.strip()
    # Extract org from GitHub URL (handles both HTTPS and SSH formats)
    org_match = re.search(r"[:/]([^/]+)/[^/]+$", remote_url)
    return bool(org_match and org_match.group(1) == current_org.replace("_", "-"))


def _start_stacked_branch(
    *,
    root: Path,
    new_branch: str,
    parent: str,
    message: str,
    no_verify: bool,
    pathspec: list[str] | None,
) -> Path:
    """Start a new stacked branch off ``parent`` carrying the working changes.

    Commits the current changes with ``message`` onto ``new_branch`` (in the main
    worktree, where the changes live), records lineage, restores ``parent`` in the
    main worktree, then creates the branch's own worktree and opens it. Returns
    the new worktree path so the caller can sync the stack from there.
    """
    # Lazy import: worktrees -> bootstrap.repo_setup -> workflow cycle at load.
    from toolbelt.editor import open_in_editor
    from toolbelt.git.stack.lineage import set_parent
    from toolbelt.git.worktrees import _worktree_path_for_name, copy_dotfiles

    wt_path = _worktree_path_for_name(name=message, repo_root=root)
    if wt_path.exists():
        logger.error(f"Error: worktree path already exists: {wt_path}")
        raise typer.Exit(1)

    # Carry the working-tree changes onto the new branch, then restore the parent
    # branch in the main worktree so the new work lives only in its own worktree.
    run(["git", "checkout", "-b", new_branch], cwd=root, exit_on_error=True)
    set_parent(new_branch, parent, root=root)
    _stage(root=root, pathspec=pathspec)
    _commit(root=root, message=message, amend=False, no_verify=no_verify)
    run(["git", "checkout", parent], cwd=root, exit_on_error=True)
    run(
        ["git", "worktree", "add", str(wt_path), new_branch],
        cwd=root,
        exit_on_error=True,
    )
    copy_dotfiles(root=root, wt_path=wt_path)
    update_repo(wt_path)
    logger.info(f"Created worktree at {wt_path}")
    open_in_editor(wt_path)
    return wt_path


def git_save(
    message: str | None,
    no_verify: bool,
    no_sync: bool,
    amend: bool,
    pathspec: list[str] | None,
    yes: bool,
) -> Path:
    """Commit (and unless ``no_sync`` sync) changes; return the commit's worktree.

    The returned path is the worktree the commit landed in — the current one, or
    a freshly created stacked-branch worktree when saving off the default branch.
    Callers that follow up (e.g. `git send` opening a PR) must act in that path.
    """
    if not message and not amend:
        raise typer.BadParameter("Commit message or --amend is required")

    # Lazy import: worktrees -> bootstrap.repo_setup -> workflow cycle at load.
    from toolbelt.git.worktrees import current_branch, repo_root

    root = repo_root()
    current = current_branch(root)
    default_branch = get_default_branch()

    if _should_start_new_branch(
        root=root, current_branch=current, default_branch=default_branch
    ):
        assert message, "Message is required when committing to a default branch"
        new_branch_name = f"{GIT_BRANCH_PREFIX}{message.replace(' ', '_').rstrip('.')}"
        if yes:
            should_create_branch = True
        else:
            should_commit = input(
                "On a default branch. "
                f"Commit to a new branch called {new_branch_name}? (y/n): "
            )
            should_create_branch = should_commit.lower() == "y"
        if not should_create_branch:
            logger.info(
                "Changes not committed. Use `git commit` to commit to a default branch."
            )
            raise typer.Exit(1)

        commit_root = _start_stacked_branch(
            root=root,
            new_branch=new_branch_name,
            parent=default_branch,
            message=message,
            no_verify=no_verify,
            pathspec=pathspec,
        )
        commit_branch = new_branch_name
    else:
        if os.getenv("CURRENT_ORG"):
            logger.info(
                f"Not on the default branch. Continuing from this branch: {current}"
            )
        _stage(root=root, pathspec=pathspec)
        # Check for conflicts with the parent branch (may unstage + abort).
        check_for_parent_branch_merge_conflicts(
            current_branch=current, root=root, yes=yes
        )
        _commit(root=root, message=message, amend=amend, no_verify=no_verify)
        commit_root = root
        commit_branch = current

    # Sync the changes
    if not no_sync:
        sync_repo(commit_root)

    if message:
        store_commit(message, current_repo_name(), current_repo_org(), commit_branch)
    staged_description = f"{len(pathspec)} files" if pathspec else "all changes"
    commit_description = "amended commit" if amend else "new commit"
    sync_description = "; synced stack + pushed" if not no_sync else "; sync skipped"
    logger.info(
        f"Git save complete: committed {staged_description}; "
        f"{commit_description} on {commit_branch}{sync_description}."
    )
    return commit_root


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
