import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

import typer

from toolbelt import logged_process
from toolbelt.git.commits import store_commit
from toolbelt.git.constants import GIT_BRANCH_PREFIX
from toolbelt.git.exec import capture, run
from toolbelt.logger import logger
from toolbelt.repos import current_repo, current_repo_name

from .branches import (
    get_current_branch_name,
    get_default_branch,
)
from .repo import current_repo_org, get_current_repo_root_path
from .repo_pull import (
    default_config_path,
    default_state_dir,
    start_background_pull,
    take_unreported_summary,
)
from .stack.forge import Forge, GhForge, candidate_from_json
from .worktrees_ops import (
    _is_worktree_dirty,
    _worktree_paths_for_branch,
    delete_branch_and_worktree,
    ensure_not_inside_worktree,
    main_worktree,
)


def update_repo(target_path: Path):
    if (target_path / ".tool-versions").exists():
        # This may fail if the plugins in .tool-versions are not installed
        logged_process.run(["asdf", "install"], check=True, cwd=target_path)
    if (target_path / "uv.lock").exists():
        logged_process.run(["uv", "sync", "--all-groups"], check=True, cwd=target_path)


def sync_repo(root: Path | None = None, *, branch: str | None = None) -> None:
    """Merge-sync the whole stack the current branch belongs to and refresh deps.

    Replaces the old git-town flow with the homegrown stack sync: each branch is
    merged parent -> child inside its own worktree and pushed (so no extra
    ``git push`` here — ``sync_stack`` pushes every branch). ``root`` defaults to
    the current worktree; pass a specific worktree to sync a stack the caller
    just moved into (e.g. a freshly created branch). ``branch`` syncs the stack
    of that branch instead of the one ``root`` has checked out.

    ``root`` itself can end up deleted by either step below: ``sync_stack``
    removes a landed branch's worktree (which can be ``root``, e.g. syncing
    from inside a branch whose own PR just merged), and so can
    ``git_branch_clean``. ``update_repo`` needs a real, still-existing path,
    so it falls back to the main worktree when ``root`` no longer exists.

    Finally it spawns a detached process that fast-forwards the repos listed
    under ``sync.repos`` (see ``repo_pull``) without blocking; the summary of
    the previous run is printed here, once, at the start of the next sync.
    """
    state_dir = default_state_dir()
    summary = take_unreported_summary(state_dir=state_dir)
    if summary:
        logger.info(summary)

    # Imported lazily: worktrees -> bootstrap.repo_setup -> workflow would be a
    # circular import at module load time.
    from toolbelt.git.stack.gh_stack import GhStackedPrs
    from toolbelt.git.stack.sync import sync_stack
    from toolbelt.git.worktrees import repo_root

    root = root or repo_root()
    main_wt = main_worktree(root)
    forge = GhForge(root)
    sync_stack(root=root, forge=forge, stacked_prs=GhStackedPrs(), branch=branch)
    git_branch_clean(root, forge=forge)
    update_repo(root if root.exists() else main_wt)
    start_background_pull(state_dir=state_dir, config_path=default_config_path())


_STACKED_MERGE_ERROR = "asynchronous merge REST API"
_ASYNC_MERGE_TIMEOUT_SECONDS = 300
_ASYNC_MERGE_POLL_SECONDS = 2


def _merge_stacked_pr(number: int) -> bool:
    """Squash-merge a PR that is part of a GitHub stack, via the async merge API.

    GitHub rejects the regular merge mutation for stacked PRs. The async endpoint
    merges the PR together with every open PR below it in the stack. Returns True
    once the PR is merged, False if it was queued in a merge queue instead.
    """
    endpoint = f"repos/{{owner}}/{{repo}}/pulls/{number}/merge-async"
    logger.warning(
        f"PR {number} is part of a GitHub stack: merging it also merges the open "
        "PRs below it."
    )
    result = json.loads(
        capture(["gh", "api", "-X", "PUT", endpoint, "-f", "merge_method=squash"])
    )
    deadline = time.monotonic() + _ASYNC_MERGE_TIMEOUT_SECONDS
    while result["status"] == "pending":
        if time.monotonic() > deadline:
            logger.error(f"Timed out waiting for PR {number} to merge.")
            raise typer.Exit(1)
        time.sleep(_ASYNC_MERGE_POLL_SECONDS)
        uuid = result["details"]["uuid"]
        result = json.loads(capture(["gh", "api", f"{endpoint}/{uuid}"]))
    if result["status"] == "failed":
        logger.error(f"PR {number} did not merge: {result['details']['message']}")
        raise typer.Exit(1)
    return result["status"] == "merged"


def git_merge(pr: str) -> None:
    """Squash-merge a PR, then sync the stack the merged branch belongs to.

    ``pr`` is passed through to ``gh pr merge``/``gh pr view`` as-is (a PR
    number, URL, or branch name). The stack synced is the merged branch's,
    whatever worktree this is invoked from, and it is synced from the main
    worktree because the merged branch's own worktree is removed by the sync.
    Invoked from inside that worktree it refuses before merging anything. If
    the branch has no tracked worktree (already removed, or never tracked),
    there's no stack to restack, so this just runs the repo-wide
    branch/lineage cleanup instead.
    """
    # Lazy import: worktrees -> bootstrap.repo_setup -> workflow would be a
    # circular import at module load time.
    from toolbelt.git.stack.worktree import worktree_paths

    viewed = json.loads(
        capture(
            [
                "gh",
                "pr",
                "view",
                pr,
                "--json",
                "headRefName,number,id,createdAt",
            ]
        )
    )
    branch = viewed["headRefName"]
    # Record which PR the branch's task landed as before the merge cleanup
    # archives it (a no-op for an untracked branch or one already linked).
    from toolbelt.git.stack import lineage

    root = get_current_repo_root_path()
    main_wt = main_worktree(root)
    ensure_not_inside_worktree(branch, root=main_wt, caller_dirs=[Path.cwd()])
    lineage.link_pr(branch, [candidate_from_json(viewed)], root=root)
    merged = logged_process.run(
        ["gh", "pr", "merge", "--squash", pr], stderr=subprocess.PIPE, text=True
    )
    if merged.returncode and _STACKED_MERGE_ERROR in merged.stderr:
        if not _merge_stacked_pr(viewed["number"]):
            logger.info(f"PR {viewed['number']} was queued; not syncing the stack.")
            return
    elif merged.returncode:
        typer.echo(merged.stderr, err=True)
        raise typer.Exit(merged.returncode)

    worktree = worktree_paths(root=root).get(branch)
    if worktree is None:
        logger.warning(
            f"No worktree tracked for '{branch}'; skipping stack sync, just "
            "cleaning up local branches."
        )
        git_branch_clean(root, forge=GhForge(root))
        return
    sync_repo(main_wt, branch=branch)


def record_branch_pr(branch: str, *, root: Path, forge: Forge) -> None:
    """Record the PR ``forge`` knows for ``branch`` as its task's PR, unless
    one is already recorded (the lookup is skipped then). Does nothing for an
    untracked branch."""
    # Lazy import: worktrees -> bootstrap.repo_setup -> workflow would be a
    # circular import at module load time.
    from toolbelt.git.stack import lineage

    if lineage.get_pr_link(branch, root=root) is None:
        lineage.link_pr(branch, asyncio.run(forge.pr_candidates(branch)), root=root)


def git_pr(
    skip_tests: bool, cwd: Path | None = None, forge: Forge | None = None
) -> None:
    # ``cwd`` targets the branch's worktree: when `git save` starts a new stacked
    # branch it lands in its own worktree, so the PR must be opened from there
    # rather than the (default-branch) directory the command was invoked in.
    from toolbelt.git.worktrees import current_branch, repo_root

    root = cwd or repo_root()
    forge = forge or GhForge(root)
    view_pr = logged_process.run(["gh", "pr", "view", "--web"], check=False, cwd=cwd)
    if view_pr.returncode == 0:
        # The PR exists: remember which one it is for this branch's task.
        record_branch_pr(current_branch(root), root=root, forge=forge)
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
    from toolbelt.git.stack.ops import push_if_unpushed

    branch = current_branch(root)
    base = get_parent(branch, root=root) or get_default_branch()
    # `gh pr create` aborts for a branch GitHub cannot see. `sync` pushes a leaf
    # only once it has a PR, so a new branch is first pushed here (after the
    # tests, so a failing run pushes nothing).
    push_if_unpushed(branch, root=root)
    logged_process.run(
        ["gh", "pr", "create", "--web", "--base", base],
        check=False,
        cwd=cwd,
    )
    # `--web` only opens the browser's form, so the PR usually does not exist
    # yet and nothing is recorded; it is when it was created by the time this
    # runs, otherwise the next `git sync` records it.
    record_branch_pr(branch, root=root, forge=forge)


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


def _branch_exists(branch: str, *, root: Path, remote: bool = False) -> bool:
    prefix = "refs/remotes" if remote else "refs/heads"
    return (
        logged_process.run(
            ["git", "show-ref", "--verify", "--quiet", f"{prefix}/{branch}"],
            cwd=root,
        ).returncode
        == 0
    )


def _unsafe_to_delete_reason(branch: str, *, root: Path) -> str | None:
    """Why a "gone" ``branch`` must be kept, or ``None`` if it's safe to delete.

    Its upstream being gone only says the remote branch was deleted — which
    also happens to PRs that were closed unmerged. So keep it when:

    - it's checked out in the main worktree (``git branch -D`` would fail),
    - a worktree holding it has uncommitted/untracked changes, or
    - it carries changes not already in ``origin``'s default branch. Merging it
      into that branch is simulated (``git merge-tree``) and must leave the
      tree unchanged — this also holds for squash merges, where the branch's
      commits themselves never become ancestors.
    """
    worktrees = _worktree_paths_for_branch(branch, root)
    if root in worktrees:
        return "checked out in the main worktree"
    if any(_is_worktree_dirty(path) for path in worktrees):
        return "its worktree has uncommitted changes"

    base = next(
        (
            f"origin/{name}"
            for name in ("main", "master")
            if _branch_exists(f"origin/{name}", root=root, remote=True)
        ),
        None,
    )
    if base is None:
        return "no origin/main or origin/master to compare against"
    merged = logged_process.run(
        ["git", "merge-tree", "--write-tree", base, branch],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    base_tree = capture(["git", "rev-parse", f"{base}^{{tree}}"], cwd=root)
    if merged.returncode != 0 or merged.stdout.split()[0] != base_tree:
        return f"it has changes not in {base}"
    return None


def git_branch_clean(root: Path | None = None, *, forge: Forge | None = None) -> None:
    """
    Delete local branches whose upstream has been removed (unless they may
    hold unmerged work or uncommitted changes; see ``_unsafe_to_delete_reason``
    — those are kept and reported), and drop stale
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
    whose branch just got merged. For the same reason, a branch whose worktree
    contains ``root`` (or the cwd) is not deleted: ``WorktreeInUseError`` is
    raised instead, asking the caller to run from another worktree.

    When ``forge`` is given, the PR of each branch it deletes is first
    recorded as its task's PR (unless already known), so the archived task
    can be joined to it by id.
    """
    # Lazy import: worktrees -> bootstrap.repo_setup -> workflow would be a
    # circular import at module load time.
    from toolbelt.git.stack import lineage

    caller_root = root or get_current_repo_root_path()
    root = main_worktree(caller_root)
    logged_process.run(["git", "fetch", "-p"], check=True, cwd=root)
    branch_list = logged_process.run(
        ["git", "branch", "-vv"],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    ).stdout.splitlines()

    deleted_branches: list[str] = []
    kept_branches: list[str] = []
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

        reason = _unsafe_to_delete_reason(branch_name, root=root)
        if reason:
            kept_branches.append(f"{branch_name} ({reason})")
            continue

        ensure_not_inside_worktree(
            branch_name, root=root, caller_dirs=[caller_root, Path.cwd()]
        )
        if forge is not None:
            record_branch_pr(branch_name, root=root, forge=forge)
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
    if kept_branches:
        logger.warning("Kept branches whose upstream is gone but may hold work:")
        for entry in kept_branches:
            logger.warning(f"  {entry}")
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

    merge_tree_result = logged_process.run(
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
        logged_process.run(["git", "reset"], check=True, cwd=root)
        logger.info("Changes unstaged. Aborting commit.")
        raise typer.Exit(1)


def _stage(*, root: Path, pathspec: list[str] | None) -> None:
    git_add_command = ["git", "add"]
    git_add_command.extend(pathspec if pathspec else ["-A"])
    logged_process.run(git_add_command, check=True, cwd=root)


def _commit(*, root: Path, message: str | None, amend: bool, no_verify: bool) -> None:
    git_commit_command = ["git", "commit"]
    if message:
        git_commit_command.extend(["-m", message])
    if amend:
        git_commit_command.append("--amend")
    if no_verify:
        git_commit_command.append("--no-verify")
    logged_process.run(git_commit_command, check=True, text=True, cwd=root)


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
    remote_url = logged_process.run(
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
    uncommitted_check = logged_process.run(
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
    logged_process.run(["git", "fetch"], check=True)

    # Check if current branch has diverged from remote
    try:
        logged_process.run(
            ["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{current_branch}"],
            check=True,
            capture_output=True,
            text=True,
        )

        # If we get here, it's safe to pull
        logger.info("Branch can be fast-forwarded. Pulling changes...")
        logged_process.run(["git", "pull"], check=True)
        logger.info("Successfully pulled changes!")

    except subprocess.CalledProcessError:
        logger.warning(
            "⚠️  Warning: Local branch has diverged from remote. Pulling might cause conflicts."
        )
        raise typer.Exit(1)
