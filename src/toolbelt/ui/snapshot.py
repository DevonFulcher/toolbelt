"""Builds `tt ui`'s API snapshot from the stack DB and the in-memory caches.

Everything here reads state that is already in memory or in the local stack
DB (no network on the request path): the statuses come from the
``StatusRefresher`` cache and the tasks' titles from the ``TaskFeed``. The
same ``live_status`` produces a branch's status for the snapshot and for the
``status`` events that keep it current.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Protocol

import typer

from toolbelt.git.stack import lineage
from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import Parents, StackStore, default_db_path
from toolbelt.git.worktrees import current_branch
from toolbelt.tasks.tracker import TaskTracker
from toolbelt.ui import api_models as api
from toolbelt.ui import branch_links, pr_states
from toolbelt.ui.claims_feed import BranchHolder
from toolbelt.ui.conflict_commit import ConflictCommit
from toolbelt.ui.live_status import Fetched, StatusRefresher
from toolbelt.ui.merge_queue import NO_MERGE_QUEUE, MergeQueueLookup
from toolbelt.ui.next_action import (
    WHO_ORDER,
    ParentContext,
    SlackContext,
    next_action,
)
from toolbelt.ui.pr_activity import PrFacts, derive_activity, format_age
from toolbelt.ui.repo_visibility import visible_repos
from toolbelt.ui.slack_tracker import NO_SLACK, SlackLookup
from toolbelt.ui.task_feed import TaskFeed

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")
_PR_URL_RE = re.compile(r"(https://github\.com/[^/]+/[^/]+)/pull/\d+")

# The comments filter's values (and what the client's sort/filter reads).
COMMENTS_OPEN = "has comments"
COMMENTS_NONE = "none"

#: The Next filter's friendly option texts. The option *values* stay the raw
#: who (what the URL and the sort use).
_NEXT_OPTION_LABELS = {
    "you": "Needs me",
    "agent": "With an agent",
    "auto": "Autonomous agent",
    "ci": "Waiting on CI",
    "others": "Waiting on others",
    "none": "Done",
}

# (filter key, label, option values) in display order.
_FILTERS: list[tuple[str, str, list[str]]] = [
    ("next", "Next", list(WHO_ORDER)),
    ("pr", "PR", [s.value for s in PrState]),
    ("ci", "CI", [s.value for s in CiState]),
    ("review", "Review", [s.value for s in ReviewState]),
    ("comments", "Comments", [COMMENTS_OPEN, COMMENTS_NONE]),
    ("merge", "Merge", ["conflicts", "clean", "none"]),
]


def filters() -> list[api.FilterSpec]:
    return [
        api.FilterSpec.model_validate(
            {
                "key": key,
                "label": label,
                "options": [
                    {
                        "value": value,
                        "label": _NEXT_OPTION_LABELS.get(value, value)
                        if key == "next"
                        else value,
                    }
                    for value in values
                ],
            }
        )
        for key, label, values in _FILTERS
    ]


class ClaimLookup(Protocol):
    """Who holds claims on a branch (``ClaimsFeed`` is one)."""

    def holders(self, repo_id: int, branch: str) -> tuple[BranchHolder, ...]: ...

    def working(self, repo_id: int, branch: str) -> bool: ...

    def autonomous(self, repo_id: int, branch: str) -> bool: ...

    def deciding(self, repo_id: int, branch: str) -> bool: ...


class _NoClaims:
    def holders(self, repo_id: int, branch: str) -> tuple[BranchHolder, ...]:
        return ()

    def working(self, repo_id: int, branch: str) -> bool:
        return False

    def autonomous(self, repo_id: int, branch: str) -> bool:
        return False

    def deciding(self, repo_id: int, branch: str) -> bool:
        return False


NO_CLAIMS: ClaimLookup = _NoClaims()


def row_id(repo_id: int, branch: str) -> str:
    return f"{repo_id}:{branch}"


# ---- stack structure ------------------------------------------------------------


def walk(parents: Parents) -> list[tuple[str, int]]:
    """Every tracked branch depth-first as ``(name, depth)``. The trunk(s)
    the stacks hang off are not tracked branches and are left out, so a
    stack starts at depth 0 with its first real branch."""
    children = lineage.children_map(parents)
    order: list[tuple[str, int]] = []

    def visit(branch: str, depth: int) -> None:
        order.append((branch, depth))
        for child in children.get(branch, []):
            visit(child, depth + 1)

    for base in lineage.roots(parents):
        for first in children.get(base, []):
            visit(first, 0)
    return order


def stack_heights(order: list[tuple[str, int]]) -> dict[str, int]:
    """Each branch -> the height of its stack (the deepest chain below the
    stack's first branch, counted in branches), from the depth-first order."""
    heights: dict[str, int] = {}
    group: list[str] = []
    deepest = 0
    for name, depth in [*order, ("", 0)]:
        if depth == 0:
            for member in group:
                heights[member] = deepest
            group, deepest = [], 0
        group.append(name)
        deepest = max(deepest, depth + 1)
    return heights


def _checked_out_branch(root: Path) -> str | None:
    try:
        return current_branch(root)
    except typer.Exit:
        # Detached HEAD (common in worktrees): nothing to mark.
        return None


def parent_context(
    repo_id: int,
    parents: Parents,
    branch: str,
    refresher: StatusRefresher,
    now: datetime,
    claims: ClaimLookup = NO_CLAIMS,
    slack: SlackLookup = NO_SLACK,
) -> ParentContext | None:
    """The tracked branch ``branch`` is stacked on, with its PR state and who
    its own next action waits on (which depends on *its* parent in turn); ``None``
    for a branch on the base branch, or while the parent's status is unknown."""
    parent = parents.get(branch)
    if parent is None or parent not in parents:
        return None
    fetched = refresher.get(repo_id, parent)
    if fetched is None or fetched.status is None:
        return None
    parent_action = next_action(
        fetched.status,
        parent_context(repo_id, parents, parent, refresher, now, claims, slack),
        claims.working(repo_id, parent),
        claims.autonomous(repo_id, parent),
        slack.context(repo_id, parent, fetched, now),
        claims.deciding(repo_id, parent),
    )
    return ParentContext(name=parent, pr=fetched.status.pr, next_who=parent_action.who)


def descendants(parents: Parents, branch: str) -> list[str]:
    """``branch`` and every branch stacked on it, at any depth. A change to
    one branch's PR changes the next action of all of these ("Blocked on
    <parent>", and who that waits on follows the parent's own next action)."""
    names = [branch]
    for name in names:
        names.extend(child for child, parent in parents.items() if parent == name)
    return names


# ---- a branch's status ------------------------------------------------------------


def _iso(time: datetime) -> str:
    return time.isoformat().replace("+00:00", "Z")


def pr_detail(facts: PrFacts, now: datetime) -> api.PrDetail:
    activity = derive_activity(facts, now)
    done = facts.state in ("MERGED", "CLOSED")
    ended = None
    if facts.state == "MERGED":
        ended = facts.merged_at
    elif facts.state == "CLOSED":
        ended = facts.closed_at

    def growing(age, since) -> api.Age | None:
        if age is None or since is None:
            return None
        return api.Age(text=format_age(age), since=_iso(since))

    return api.PrDetail(
        title=facts.title,
        # A finished PR's age is fixed, so it carries no start to count from.
        pr_age=api.Age(
            text=format_age(activity.pr_age),
            since=None if done else _iso(facts.created_at),
        ),
        draft_age=growing(activity.draft_age, activity.draft_since),
        review_request_age=growing(
            activity.review_request_age, activity.review_request_since
        ),
        ci_failures=activity.ci_failure_count,
        additions=facts.additions,
        deletions=facts.deletions,
        created=_iso(facts.created_at),
        ended=_iso(ended) if ended else None,
        is_draft=facts.is_draft,
        events=[
            api.TimelineEvent.model_validate(
                {
                    "t": _iso(event.time),
                    "kind": event.kind,
                    "actor": event.actor,
                    "detail": event.detail,
                    "ci": event.ci,
                    "subject": event.subject,
                    "ci_done": _iso(event.ci_done) if event.ci_done else None,
                }
            )
            for event in activity.timeline
        ],
    )


def conflict_info(conflict: ConflictCommit, pr_url: str | None) -> api.ConflictInfo:
    match = _PR_URL_RE.match(pr_url) if pr_url else None
    return api.ConflictInfo(
        sha=conflict.sha,
        subject=conflict.subject,
        url=f"{match.group(1)}/commit/{conflict.sha}" if match else None,
        files=list(conflict.files),
        source=conflict.source,
        step=conflict.step,
        steps=conflict.steps,
    )


def loaded_status(
    status: BranchStatus,
    *,
    parent: ParentContext | None,
    facts: PrFacts | None,
    now: datetime,
    claimed: bool = False,
    autonomous: bool = False,
    conflict: ConflictCommit | None = None,
    slack: SlackContext | None = None,
    deciding: bool = False,
) -> api.LoadedStatus:
    """``parent`` is the stack context for the next action; ``facts`` (with
    ``now``) adds the PR's history as ``detail``; ``claimed``: a live working
    holder has claimed the branch; ``autonomous``: so has an autonomous
    agent; ``slack``: where the PR stands in Slack."""
    has_pr = status.pr is not PrState.NONE
    match = _PR_NUMBER_RE.search(status.url) if status.url else None
    merge = "none"
    if status.pr in (PrState.OPEN, PrState.DRAFT):
        merge = "conflicts" if status.has_conflicts else "clean"
    open_comments = status.open_comments if has_pr else 0
    open_comments_bot = status.bot_comments if has_pr else 0
    action = next_action(status, parent, claimed, autonomous, slack, deciding)
    chips = pr_states.current_states(
        pr=status.pr.value,
        ci=status.ci.value,
        review=status.review.value,
        open_comments=open_comments,
        merge=merge,
        next_kind=action.kind,
        next_who=action.who,
        next_label=action.label,
        next_would_be=action.would_be.label if action.would_be else None,
    )
    return api.LoadedStatus.model_validate(
        {
            "load": "loaded",
            "pr": status.pr,
            "pr_number": int(match.group(1)) if match and has_pr else None,
            "pr_url": status.url if has_pr else None,
            "ci": status.ci,
            "review": status.review,
            "merge": merge,
            "open_comments": open_comments,
            "open_comments_bot": open_comments_bot,
            "next": {
                "label": action.label,
                "who": action.who,
                "kind": action.kind,
                "would_be": {
                    "label": action.would_be.label,
                    "who": action.would_be.who,
                    "kind": action.would_be.kind,
                }
                if action.would_be
                else None,
            },
            "conflict": conflict_info(conflict, status.url) if conflict else None,
            "detail": pr_detail(facts, now) if facts is not None else None,
            "now": [
                {"dimension": c["dimension"], "value": c["value"], **c["args"]}
                for c in chips
            ],
        }
    )


def live_status(
    fetched: Fetched | None,
    parent: ParentContext | None,
    now: datetime,
    claimed: bool = False,
    autonomous: bool = False,
    slack: SlackContext | None = None,
    deciding: bool = False,
) -> api.LoadingStatus | api.FailedStatus | api.LoadedStatus:
    if fetched is None:
        return api.LoadingStatus(load="loading")
    if fetched.status is None:
        return api.FailedStatus(load="failed")
    return loaded_status(
        fetched.status,
        parent=parent,
        facts=fetched.facts,
        now=now,
        claimed=claimed,
        autonomous=autonomous,
        conflict=fetched.conflict,
        slack=slack,
        deciding=deciding,
    )


# ---- the snapshot --------------------------------------------------------------------


def build_snapshot(
    cwd_root: Path | None,
    *,
    tracker: TaskTracker | None,
    refresher: StatusRefresher,
    task_feed: TaskFeed,
    untracked: api.UntrackedPrs,
    now: datetime,
    version: int,
    claims: ClaimLookup = NO_CLAIMS,
    extras: api.CardExtras | None = None,
    agents: api.Agents | None = None,
    spend: list[api.Spend] | None = None,
    webhooks: list[api.RepoWebhook] | None = None,
    slack: SlackLookup = NO_SLACK,
    merge_queue: MergeQueueLookup = NO_MERGE_QUEUE,
) -> api.Snapshot:
    """Every repo with tracked branches, and all their branches: repos in
    name order, each repo's branches depth-first."""
    cwd_common_dir = (
        lineage.repo_identity(cwd_root).git_common_dir if cwd_root else None
    )
    store = StackStore(default_db_path())
    try:
        repos: list[api.Repo] = []
        branches: list[api.Branch] = []
        fetched: list[api.FetchedBranch] = []
        for repo in sorted(visible_repos(store.repos()), key=lambda r: (r.name, r.id)):
            parents = store.all_parents(repo.id)
            if not parents:
                continue
            repos.append(
                api.Repo(
                    id=repo.id,
                    name=repo.name,
                    path=str(repo.path),
                    path_missing=not repo.path.is_dir(),
                )
            )
            current = (
                _checked_out_branch(cwd_root)
                if cwd_root and repo.git_common_dir == cwd_common_dir
                else None
            )
            order = walk(parents)
            heights = stack_heights(order)
            kids = lineage.children_map(parents)
            for name, depth in order:
                links = store.get_links(repo.id, name)
                started_at = store.get_started_at(repo.id, name)
                owner = branch_links.owning_tracker(
                    links.task_tracker if links else None, tracker
                )
                parent = parents.get(name)
                looked_up = refresher.get(repo.id, name)
                if looked_up is not None and (
                    looked_up.wall_at is not None or looked_up.error is not None
                ):
                    fetched.append(
                        api.FetchedBranch(
                            branch_id=row_id(repo.id, name),
                            fetched_at=(
                                _iso(looked_up.wall_at) if looked_up.wall_at else None
                            ),
                            error=looked_up.error,
                        )
                    )
                fetchable = owner is not None and bool(links and links.task)
                task = branch_links.task_link(
                    links.task if links else None, tracker=owner
                )
                agent = branch_links.agent_link(links.agent_link if links else None)
                branches.append(
                    api.Branch(
                        id=row_id(repo.id, name),
                        repo_id=repo.id,
                        name=name,
                        depth=depth,
                        parent_id=row_id(repo.id, parent)
                        if parent in parents
                        else None,
                        parent=parent if parent in parents else None,
                        order=len(branches),
                        current=name == current,
                        stack_pos=depth + 1,
                        stack_height=heights[name],
                        children=[
                            api.ChildRef(id=row_id(repo.id, child), name=child)
                            for child in kids.get(name, [])
                        ],
                        task=_link(task),
                        task_fetchable=fetchable,
                        task_live=task_feed.get(repo.id, name) if fetchable else None,
                        agent=_link(agent),
                        claim=branch_claim(claims.holders(repo.id, name), now),
                        slack=slack.info(repo.id, name, looked_up, now),
                        merge_queue=merge_queue.info(repo.id, name),
                        started_at=_iso(started_at) if started_at else None,
                        status=live_status(
                            looked_up,
                            parent_context(
                                repo.id, parents, name, refresher, now, claims, slack
                            ),
                            now,
                            claims.working(repo.id, name),
                            claims.autonomous(repo.id, name),
                            slack.context(repo.id, name, looked_up, now),
                            claims.deciding(repo.id, name),
                        ),
                    )
                )
        return api.Snapshot(
            version=version,
            age=refresher.seconds_since_refresh(),
            repos=repos,
            branches=branches,
            catalog=api.build_catalog(),
            filters=filters(),
            untracked=untracked,
            fetched=fetched,
            extras=extras,
            agents=agents,
            spend=spend or [],
            webhooks=webhooks or [],
        )
    finally:
        store.close()


def branch_claim(holders: tuple[BranchHolder, ...], now: datetime) -> api.BranchClaim:
    return api.BranchClaim(
        holders=[
            api.ClaimHolder.model_validate(
                {
                    "holder": h.holder,
                    "kind": h.kind,
                    "state": h.state,
                    "link": _link(branch_links.agent_link(h.link)),
                    "age": {
                        "text": format_age(now - h.started_at),
                        "since": _iso(h.started_at),
                    },
                    "stale": h.stale,
                }
            )
            for h in holders
        ]
    )


def _link(link: branch_links.Link | None) -> api.Link | None:
    return api.Link(label=link.label, href=link.href) if link else None
