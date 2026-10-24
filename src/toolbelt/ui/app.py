"""The `tt ui` web app: every known repo's stacks, live, read-only.

The server is a JSON API plus the built web client (``web/``, served from
``static/dist``):

* ``GET /api/snapshot``: every tracked branch of every repo (stack structure,
  links, live status, the state catalog), straight from the stack DB plus the
  in-memory caches (no network on the request path); see ``snapshot.py`` and
  ``api_models.py``;
* ``GET /api/spend?days=N``: spend on this machine, from the transcripts
  ``spend_feed.py`` keeps ingested, by window, model and task;
* ``GET /api/aggregate?weeks=N[&repo=<id or name>]``: how work has flowed over the last N weeks
  (throughput, lead times, status mix), computed from cached history; see
  ``aggregate.py`` and ``aggregate_feed.py``;
* ``GET /api/events``: a Server-Sent Events stream of typed events that keep a
  snapshot current:

  - ``hello``: sent on connect with the current event version, so a browser
    that reconnects knows whether it missed anything;
  - ``status``: one branch's new live status (also sent for the branches
    stacked on it, whose next action depends on it);
  - ``task``: a branch's task title/status was (re)fetched;
  - ``structure``: the stack DB was written (new branch, re-parenting, new
    links), so the page re-fetches the snapshot;
  - ``claims``: some branches' claim holders changed (an agent started or
    stopped working on them, or a claim expired); carries each affected
    branch's claim and re-derived status, including the branches stacked on
    it (see ``claims_feed.py``, ``task_claims``);
  - ``refreshed``: a polling pass finished (drives "updated Ns ago");
  - ``untracked``: the list of the user's open PRs that are not tracked
    branches changed (see ``untracked_prs.py``).

A background ``StatusRefresher`` (``live_status.py``) keeps the status cache
current by polling GitHub (one GraphQL call per branch, see ``pr_fetch.py``)
while any browser is connected; a ``TaskFeed`` does the same for tasks and
an ``UntrackedPrFeed`` (slowly) for the PRs toolbelt does not track.
``AggregateService`` warms 12 weeks, then refreshes the widest requested
window every fifteen minutes.

The status lookup (``fetch``), the task tracker and the clocks are
``create_app`` arguments, so tests can inject them.
"""

import asyncio
import contextlib
import dataclasses
import functools
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from toolbelt.autonomous_agents.auth import claude_signed_in
from toolbelt.autonomous_agents.branch_state import RepoFactsCache, inspect_branch
from toolbelt.autonomous_agents.ci_history import ci_explainer
from toolbelt.autonomous_agents.dispatcher import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    Dispatcher,
    DispatchTask,
)
from toolbelt.autonomous_agents.limits import (
    AGENTS_ENABLED_SETTING,
    AGENTS_LAUNCH_SETTING,
    load_limits,
)
from toolbelt.autonomous_agents.questions import RunQuestions
from toolbelt.autonomous_agents.runner import AgentRunner
from toolbelt.autonomous_agents.screen import read_screen_state
from toolbelt.decisions.book import AlreadyAnswered, DecisionBook, NoSuchDecision
from toolbelt.decisions.model import Decision, InvalidDecision
from toolbelt.diff_review.lsp import PYTHON_SERVER, LspSessions
from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack.status import CiState, PrState
from toolbelt.git.stack.store import (
    AgentRun,
    MergeQueueRow,
    Parents,
    SlackPostRow,
    SlackSessionRow,
    StackStore,
    default_db_path,
    state_dir,
)
from toolbelt.task_claims.claim_file import CLAIMS_DIRNAME
from toolbelt.task_claims.resolution import TrackedTask, git_worktrees, tracked_tasks
from toolbelt.tasks.config import configured_tracker
from toolbelt.tasks.tracker import TaskTracker
from toolbelt.token_spend.transcripts import projects_dir
from toolbelt.ui import aggregate as aggregate_view
from toolbelt.ui import api_models as api
from toolbelt.ui import branch_links
from toolbelt.ui import spend_feed as spend_module
from toolbelt.ui.agents_view import build_agents, runs_window_start
from toolbelt.ui.aggregate_feed import (
    AggregateService,
    PageRunner,
    StoredHistory,
    page_runner,
    read_history,
)
from toolbelt.ui.claims_feed import (
    CLAIMS_TICK_SECONDS,
    SPAN_MAX_GAP,
    BranchKey,
    ClaimObservation,
    ClaimsFeed,
)
from toolbelt.ui.client_build import CLIENT_DIST, served_build
from toolbelt.ui.conflict_commit import conflict_for
from toolbelt.ui.forwarder_ledger import SettingsLedger
from toolbelt.ui.github_requests import (
    ConditionalAnswer,
    GithubRequests,
    RateLimited,
)
from toolbelt.ui.github_webhooks import affected
from toolbelt.ui.live_status import (
    DEFAULT_CONCURRENCY,
    DEFAULT_FULL_INTERVAL_SECONDS,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_SETTLED_INTERVAL_SECONDS,
    DEFAULT_TICK_SECONDS,
    EventHub,
    Fetched,
    StatusFetcher,
    StatusRefresher,
    StructureWatcher,
    TrackedBranch,
)
from toolbelt.ui.lookup_cache import LookupCache
from toolbelt.ui.merge_pr import MergeOutcome, run_toolbelt_merge
from toolbelt.ui.merge_queue import (
    DEFAULT_TICK_SECONDS as DEFAULT_MERGE_QUEUE_TICK_SECONDS,
)
from toolbelt.ui.merge_queue import LandResult, MergeQueue, MergeQueueRefused
from toolbelt.ui.next_action import MERGE, next_action
from toolbelt.ui.pr_fetch import (
    GraphqlRunner,
    PrSnapshot,
    fetch_snapshot,
    graphql_runner,
)
from toolbelt.ui.pr_gate import GithubChangeGate, PrGate
from toolbelt.ui.pushed_views import PushedViews, conversation_of
from toolbelt.ui.repo_visibility import visible_repos
from toolbelt.ui.review_routes import review_router
from toolbelt.ui.slack_config import SlackConfig
from toolbelt.ui.slack_proxy import SlackProxy
from toolbelt.ui.slack_service import SlackNotApplicable, SlackService, SlackUnknown
from toolbelt.ui.slack_tracker import (
    NO_SLACK,
    BumpWatcher,
    SlackLookup,
    SlackTracker,
)
from toolbelt.ui.snapshot import (
    branch_claim,
    build_snapshot,
    descendants,
    live_status,
    parent_context,
    row_id,
)
from toolbelt.ui.status_transitions import StatusTransitionRecorder
from toolbelt.ui.task_cache import TaskInfoCache
from toolbelt.ui.task_feed import LinkedTask, TaskFeed
from toolbelt.ui.task_status_sync import TaskStatusSync
from toolbelt.ui.untracked_prs import (
    DEFAULT_INTERVAL_SECONDS as DEFAULT_UNTRACKED_INTERVAL_SECONDS,
)
from toolbelt.ui.untracked_prs import (
    RemoteLookup,
    SearchRunner,
    TrackedRepo,
    UntrackedPrFeed,
    origin_remote,
    repo_slug,
    search_runner,
)
from toolbelt.ui.webhook_forwarders import GithubHooks, WebhookForwarders

# Sent when no event has gone out for this long, so intermediaries keep the
# stream open and a dead client is noticed.
_KEEPALIVE_SECONDS = 15.0


async def fetch_status(branch: str, root: Path, graphql: GraphqlRunner) -> PrSnapshot:
    snapshot = await fetch_snapshot(branch, root, graphql=graphql)
    facts = snapshot.facts
    conflict = await asyncio.to_thread(
        conflict_for,
        root,
        branch,
        base_ref=facts.base_ref if facts else None,
        head_oid=facts.head_oid if facts else None,
        pr_conflicts=snapshot.status.has_conflicts,
    )
    return replace(snapshot, conflict=conflict)


@functools.cache
def _tracker_from_config(config_file: Path) -> TaskTracker | None:
    # Built once per config path so the tracker's HTTP client and resolved
    # token are reused across requests.
    return configured_tracker(config_file)


def task_tracker() -> TaskTracker | None:
    return _tracker_from_config(default_config_path())


def _all_parents(repo_id: int) -> Parents:
    store = StackStore(default_db_path())
    try:
        return store.all_parents(repo_id)
    finally:
        store.close()


def _tracked_branches() -> list[TrackedBranch]:
    """Every tracked branch whose checkout exists: what is worth polling."""
    store = StackStore(default_db_path())
    try:
        return [
            TrackedBranch(repo_id=repo.id, branch=name, path=repo.path)
            for repo in visible_repos(store.repos())
            if repo.path.is_dir()
            for name in store.all_parents(repo.id)
        ]
    finally:
        store.close()


def _save_slack_posts(rows: list[SlackPostRow]) -> None:
    store = StackStore(default_db_path())
    try:
        store.save_slack_posts(rows)
    finally:
        store.close()


def _record_slack_session(row: SlackSessionRow) -> None:
    store = StackStore(default_db_path())
    try:
        store.add_slack_session(row)
    finally:
        store.close()


class LandRefused(Exception):
    """The branch's PR cannot be merged now; ``status`` is the HTTP status the
    Merge button's request answers with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _merge_queue_entries() -> list[MergeQueueRow]:
    store = StackStore(default_db_path())
    try:
        return store.merge_queue()
    finally:
        store.close()


def _add_to_merge_queue(row: MergeQueueRow) -> None:
    store = StackStore(default_db_path())
    try:
        store.add_to_merge_queue(row)
    finally:
        store.close()


def _remove_from_merge_queue(repo_id: int, branch: str) -> None:
    store = StackStore(default_db_path())
    try:
        store.remove_from_merge_queue(repo_id, branch)
    finally:
        store.close()


def _slack_posts() -> list[SlackPostRow]:
    store = StackStore(default_db_path())
    try:
        return store.slack_posts()
    finally:
        store.close()


def _registered_repos() -> list[tuple[int, Path, str | None]]:
    """Every registered repo as ``(id, checkout path, remote URL)``."""
    store = StackStore(default_db_path())
    try:
        return [
            (repo.id, repo.path, repo.remote_url)
            for repo in visible_repos(store.repos())
        ]
    finally:
        store.close()


def _tracked_repos() -> list[TrackedRepo]:
    """Every registered repo with its tracked branches (what the callout of
    untracked PRs excludes)."""
    store = StackStore(default_db_path())
    try:
        return [
            TrackedRepo(
                path=repo.path,
                remote_url=repo.remote_url,
                branches=frozenset(store.all_parents(repo.id)),
            )
            for repo in visible_repos(store.repos())
        ]
    finally:
        store.close()


def _linked_tasks(tracker: TaskTracker | None) -> list[LinkedTask]:
    """Every tracked branch's stored task ref that the configured tracker
    owns: the only refs that ever reach a tracker."""
    if tracker is None:
        return []
    store = StackStore(default_db_path())
    try:
        tasks = []
        for repo in visible_repos(store.repos()):
            for name in store.all_parents(repo.id):
                links = store.get_links(repo.id, name)
                if links is None or not links.task:
                    continue
                owner = branch_links.owning_tracker(links.task_tracker, tracker)
                if owner is not None:
                    tasks.append(LinkedTask(repo.id, name, links.task, owner))
        return tasks
    finally:
        store.close()


def _linked_tracker(
    tracker: TaskTracker | None, repo_id: int, branch: str
) -> tuple[str, TaskTracker] | None:
    """The branch's task ref and the configured tracker that owns it."""
    if tracker is None:
        return None
    store = StackStore(default_db_path())
    try:
        links = store.get_links(repo_id, branch)
    finally:
        store.close()
    if links is None or not links.task:
        return None
    owner = branch_links.owning_tracker(links.task_tracker, tracker)
    return (links.task, owner) if owner is not None else None


def _task_status_moved(repo_id: int, branch: str, stage: str, pr_number: int) -> bool:
    store = StackStore(default_db_path())
    try:
        return store.task_status_moved(repo_id, branch, stage, pr_number)
    finally:
        store.close()


def _record_task_status_move(
    repo_id: int, branch: str, stage: str, pr_number: int, at: datetime
) -> None:
    store = StackStore(default_db_path())
    try:
        store.record_task_status_move(repo_id, branch, stage, pr_number, at)
    finally:
        store.close()


def _claims_dir() -> Path:
    return state_dir() / CLAIMS_DIRNAME


def _record_claim(observation: ClaimObservation) -> None:
    store = StackStore(default_db_path())
    try:
        store.record_claim_seen(
            holder=observation.holder,
            session_id=observation.session_id,
            agent_id=observation.agent_id,
            holder_kind=observation.holder_kind,
            claim_kind=observation.claim_kind,
            target=observation.target,
            task_id=observation.task_id,
            touched_at=observation.touched_at,
            seen_at=observation.seen_at,
            max_gap=SPAN_MAX_GAP,
        )
    finally:
        store.close()


def _dispatch_tasks() -> list[DispatchTask]:
    store = StackStore(default_db_path())
    try:
        paths = {repo.id: repo.path for repo in visible_repos(store.repos())}
        return [
            DispatchTask(task=task, repo_path=paths[task.repo_id])
            for task in tracked_tasks(store, git_worktrees)
            if task.repo_id in paths and paths[task.repo_id].is_dir()
        ]
    finally:
        store.close()


def _agent_runs(now: datetime) -> list[AgentRun]:
    store = StackStore(default_db_path())
    try:
        return store.agent_runs(since=runs_window_start(now))
    finally:
        store.close()


async def _start_run(runner: AgentRunner, decision) -> None:
    await runner.start(decision)


async def _resume_run(runner: AgentRunner, answered, decision, prior) -> None:
    await runner.resume(answered, decision, prior)


def _abandon_agent_runs(now: datetime) -> None:
    """Runs still marked running belong to a toolbelt that stopped."""
    store = StackStore(default_db_path())
    try:
        store.abandon_running_agent_runs(now)
    finally:
        store.close()


def _setting_on(key: str) -> bool:
    store = StackStore(default_db_path())
    try:
        return store.get_setting(key, "false") == "true"
    finally:
        store.close()


def _write_setting(key: str, value: str) -> None:
    store = StackStore(default_db_path())
    try:
        store.set_setting(key, value)
    finally:
        store.close()


def _agents_enabled() -> bool:
    return _setting_on(AGENTS_ENABLED_SETTING)


def _agents_launching() -> bool:
    return _setting_on(AGENTS_LAUNCH_SETTING)


def _claim_tasks() -> list[TrackedTask]:
    store = StackStore(default_db_path())
    try:
        return tracked_tasks(store, git_worktrees)
    finally:
        store.close()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps({'type': event, **data})}\n\n"


def create_app(
    cwd_root: Path | None = None,
    *,
    fetch: StatusFetcher | None = None,
    tracker: Callable[[], TaskTracker | None] = task_tracker,
    task_ttl_seconds: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    refresh_interval: float = DEFAULT_INTERVAL_SECONDS,
    settled_refresh_interval: float = DEFAULT_SETTLED_INTERVAL_SECONDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    tick: float = DEFAULT_TICK_SECONDS,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    db_version: Callable[[], int] | None = None,
    keepalive_seconds: float = _KEEPALIVE_SECONDS,
    client_dist: Path = CLIENT_DIST,
    search_open_prs: SearchRunner | None = None,
    remote_of: RemoteLookup = origin_remote,
    untracked_interval: float = DEFAULT_UNTRACKED_INTERVAL_SECONDS,
    search_history: PageRunner | None = None,
    stored_history: Callable[[], StoredHistory] = read_history,
    claims_dir: Callable[[], Path] = _claims_dir,
    claim_tasks: Callable[[], list[TrackedTask]] = _claim_tasks,
    record_claim: Callable[[ClaimObservation], None] = _record_claim,
    claims_tick: float | None = None,
    transcripts_dir: Callable[[], Path] = projects_dir,
    spend_interval: float = spend_module.DEFAULT_INTERVAL_SECONDS,
    dispatcher: Dispatcher | None = None,
    merger: Callable[[Path, str], MergeOutcome] = run_toolbelt_merge,
    github: GithubRequests | None = None,
    slack_settings: SlackConfig | None = None,
    slack_proxy: SlackProxy | None = None,
    merge_queue_tick: float = DEFAULT_MERGE_QUEUE_TICK_SECONDS,
    change_gate: bool = False,
    lookup_cache: LookupCache | None = None,
    transitions: StatusTransitionRecorder | None = None,
    full_refresh_interval: float = DEFAULT_FULL_INTERVAL_SECONDS,
    webhook_url: str | None = None,
    forwarders: WebhookForwarders | None = None,
    decisions: DecisionBook | None = None,
    review_lsp_command: Sequence[str] = PYTHON_SERVER,
) -> FastAPI:
    """``cwd_root``: the repo `tt ui` was launched from; its checked-out
    branch is marked current, as `tt g tree` does. ``fetch`` looks up a
    branch's PR status; ``tracker`` returns the configured task tracker;
    ``db_version`` reports when the stack DB changes (default: ``PRAGMA
    data_version`` on a connection held while serving). ``clock`` times the
    polling and caches; ``wall_clock`` (UTC) dates the ages shown. Fetched
    task info is cached for ``task_ttl_seconds``. Polling carries on for
    ``grace_seconds`` after the last browser disconnects. The background
    pollers start with the app's lifespan, so an app that is not served (a
    bare ``TestClient``) never polls; tests drive ``app.state.refresher``,
    ``app.state.task_feed`` and ``app.state.structure_watcher`` directly.
    ``client_dist`` is the built web client. ``search_open_prs`` lists the
    user's open PRs (one GitHub call per ``untracked_interval``) and
    ``remote_of`` looks up a checkout's origin: see ``untracked_prs.py``.
    ``slack_settings`` is the ``slack`` section of the config (``None``:
    Slack is off and a task's next action ignores it; see ``slack_config.py``);
    ``slack_proxy`` posts the messages (default: a Haiku session, see
    ``slack_proxy.py``); ``merge_queue_tick`` is how often queued merges are
    checked (see ``merge_queue.py``).
    ``github`` is the client every GitHub request goes through (default: one
    over the signed-in ``gh``'s token, see ``github_requests.py``).
    ``change_gate`` lets a status poll skip the full lookup of a PR GitHub says
    is unchanged (see ``pr_gate.py``); without it every poll is a full lookup.
    ``lookup_cache`` keeps each branch's last good lookup across restarts (see
    ``lookup_cache.py``); without it they are only in memory. ``transitions``
    records when a branch's conflict and CI status change (see
    ``status_transitions.py``); without it nothing is recorded.
    ``webhook_url`` is where ``gh webhook forward`` replays GitHub's webhooks
    to (this server's ``/api/github-webhook``): given, webhooks are forwarded
    for the repos the user administers (see ``webhook_forwarders.py``);
    ``forwarders`` replaces that manager (tests).
    ``search_history`` pages through the user's PRs for the aggregate view
    (kept fresh incrementally, see ``AggregateService``) and
    ``stored_history`` reads the
    stack DB's tracked and archived branches: see ``aggregate_feed.py``.
    ``claims_dir`` is where agents' claim files are (re-read every
    ``claims_tick`` seconds, browser or not; default: ``tick``),
    ``claim_tasks`` lists the tracked branches a claim can resolve to, and
    ``record_claim`` stores each claim read: see ``claims_feed.py``.
    ``transcripts_dir`` is where Claude Code's session transcripts are, read
    every ``spend_interval`` seconds for the spend view: see
    ``spend_feed.py``. ``dispatcher`` decides which tasks an autonomous agent
    should be on (default: one over the real PR lookups, git, claims, screen
    and sign-in; it only decides, see ``autonomous_agents``).
    ``review_lsp_command`` starts the language server the diff review's hover
    and go-to-definition ask (see ``diff_review.lsp``)."""
    review_sessions = LspSessions(review_lsp_command)
    task_cache = TaskInfoCache(ttl_seconds=task_ttl_seconds, clock=clock)
    hub = EventHub(clock=clock, grace=grace_seconds)

    def branch_status(repo_id: int, parents: Parents, name: str, now: datetime):
        fetched = refresher.get(repo_id, name)
        return live_status(
            fetched,
            parent_context(repo_id, parents, name, refresher, now, claims, slack),
            now,
            claims.working(repo_id, name),
            claims.autonomous(repo_id, name),
            slack.context(repo_id, name, fetched, now),
            claims.deciding(repo_id, name),
        )

    def publish_claims(changed: set[BranchKey]) -> None:
        """One event for every changed branch and the branches stacked on
        them: whether a branch is claimed decides its next action, and a
        child's follows its parent's."""
        now = wall_clock()
        affected: dict[BranchKey, Parents] = {}
        for repo_id, branch in sorted(changed):
            parents = _all_parents(repo_id)
            for name in descendants(parents, branch):
                affected[(repo_id, name)] = parents
        if not affected:
            return
        hub.publish(
            "claims",
            {
                "branches": [
                    {
                        "branch_id": row_id(repo_id, name),
                        "claim": branch_claim(
                            claims.holders(repo_id, name), now
                        ).model_dump(mode="json"),
                        "status": branch_status(repo_id, parents, name, now).model_dump(
                            mode="json"
                        ),
                    }
                    for (repo_id, name), parents in affected.items()
                ]
            },
        )

    def publish_branches(repo_id: int, parents: Parents, names: list[str]) -> None:
        """A ``status`` event (with its Slack and merge-queue standing) for each
        of ``names``."""
        now = wall_clock()
        for name in names:
            looked_up = refresher.get(repo_id, name)
            info = slack.info(repo_id, name, looked_up, now)
            queue_info = merge_queue.info(repo_id, name)
            hub.publish(
                "status",
                {
                    "branch_id": row_id(repo_id, name),
                    "status": branch_status(repo_id, parents, name, now).model_dump(
                        mode="json"
                    ),
                    "slack": info.model_dump(mode="json") if info else None,
                    "merge_queue": queue_info.model_dump(mode="json")
                    if queue_info
                    else None,
                },
            )

    def publish_status(branch: TrackedBranch, fetched: Fetched) -> None:
        # The cache already holds ``fetched``, so parent contexts see it.
        parents = _all_parents(branch.repo_id)
        publish_branches(branch.repo_id, parents, descendants(parents, branch.branch))

    def publish_slack(changed: set[BranchKey]) -> None:
        """What a branch's PR is to do next in Slack changes its own status
        and, through its parent, that of the branches stacked on it."""
        for repo_id, branch in sorted(changed):
            parents = _all_parents(repo_id)
            publish_branches(repo_id, parents, descendants(parents, branch))

    decision_book = decisions or DecisionBook(wall_clock=wall_clock)
    #: The branches that have a question the user has not answered.
    deciding_keys: set[BranchKey] = set()

    def reload_decisions() -> set[BranchKey]:
        """Reads which branches have an unanswered question; returns those
        that changed."""
        pending = {
            (d.repo_id, d.branch) for d in decision_book.all() if d.status == "pending"
        }
        changed = pending ^ deciding_keys
        deciding_keys.clear()
        deciding_keys.update(pending)
        return changed

    def refresh_decisions() -> None:
        """Updates the cards of the branches whose questions changed (a decision
        was proposed, answered or deleted, here or by another process)."""
        changed = reload_decisions()
        if changed:
            publish_slack(changed)

    reload_decisions()

    claims = ClaimsFeed(
        directory=claims_dir,
        tasks=claim_tasks,
        on_change=publish_claims,
        record=record_claim,
        wall_clock=wall_clock,
        tick=min(tick, CLAIMS_TICK_SECONDS) if claims_tick is None else claims_tick,
        deciding=lambda repo_id, branch: (repo_id, branch) in deciding_keys,
    )

    spend = spend_module.SpendFeed(
        db_path=default_db_path,
        projects=transcripts_dir,
        wall_clock=wall_clock,
        interval=spend_interval,
    )

    limits = load_limits(default_config_path())
    requests = github or GithubRequests(clock=clock, wall_clock=wall_clock)
    repo_slugs: dict[int, str | None] = {}

    async def slug_of(repo_id: int) -> str | None:
        """``owner/name`` of a registered repo's GitHub remote."""
        if repo_id not in repo_slugs:
            for registered_id, path, remote_url in _registered_repos():
                repo_slugs[registered_id] = repo_slug(remote_url) or repo_slug(
                    await remote_of(path)
                )
        return repo_slugs.get(repo_id)

    async def slug_of_path(path: Path) -> str | None:
        for repo_id, repo_path, _ in _registered_repos():
            if repo_path == path:
                return await slug_of(repo_id)
        return repo_slug(await remote_of(path))

    async def is_admin(slug: str) -> bool:
        try:
            repo = await requests.get_json(f"repos/{slug}", caller="admin")
        except RateLimited:
            raise
        except RuntimeError:
            return False  # a repo the token cannot see is not one to forward
        return bool(repo.get("permissions", {}).get("admin"))

    async def conditional_get(path: str, etag: str | None) -> ConditionalAnswer:
        return await requests.conditional_get(path, etag, caller="gate")

    if fetch is None:
        pr_graphql = graphql_runner(requests, slug_of_path)

        async def fetch_with_runner(branch: str, root: Path) -> PrSnapshot:
            return await fetch_status(branch, root, pr_graphql)

        fetch = fetch_with_runner
    search_open_prs = search_open_prs or search_runner(requests)
    search_history = search_history or page_runner(requests)

    runner = AgentRunner(
        db_path=default_db_path,
        claims_dir=claims_dir,
        limits=limits,
        wall_clock=wall_clock,
    )

    async def shared_lookup(branch: str, root: Path) -> PrSnapshot:
        # The agents read PRs through the status refresher, so a task is looked
        # up once however many ask: with the same gate, saved lookups and
        # rate-limit handling, and no second cache of GitHub's answers.
        for repo_id, repo_path, _ in _registered_repos():
            if repo_path == root:
                return await refresher.snapshot(
                    TrackedBranch(repo_id=repo_id, branch=branch, path=root),
                    max_age=DEFAULT_POLL_INTERVAL_SECONDS,
                )
        raise LookupError(f"{root} is not a registered repo")

    if dispatcher is None:
        repo_facts = RepoFactsCache()
        dispatcher = Dispatcher(
            tasks=_dispatch_tasks,
            fetch=shared_lookup,
            holders=claims.holders,
            inspect=lambda item: inspect_branch(
                item.repo_path, item.task.branch, item.task.worktree, repo_facts
            ),
            runs=lambda: _agent_runs(wall_clock()),
            enabled=_agents_enabled,
            limits=limits,
            screen=read_screen_state,
            signed_in=claude_signed_in,
            wall_clock=wall_clock,
            launch=lambda decision: _start_run(runner, decision),
            launching=_agents_launching,
            questions=RunQuestions(lambda: StackStore(default_db_path())),
            resume=lambda answered, decision, prior: _resume_run(
                runner, answered, decision, prior
            ),
            explain_ci=ci_explainer(
                lambda query, variables: requests.graphql(
                    query, variables, caller="ci_history"
                ),
                slug_of_path,
            ),
        )

    views = PushedViews(
        hub=hub,
        dispatcher=dispatcher,
        runner=runner,
        spend=spend,
        db_path=default_db_path,
        projects=transcripts_dir,
        enabled=_agents_enabled,
        launching=_agents_launching,
        wall_clock=wall_clock,
        repo_slug=lambda repo_id: slug_of_repo(repo_id),
    )
    dispatcher.on_tick = lambda: views.notify("agents", "extras")
    runner.on_change = lambda: views.notify("agents", "extras")
    spend.on_ingest = lambda: views.notify("spend", "extras")

    async def forwarded_slugs() -> set[str]:
        resolved = {await slug_of(repo_id) for repo_id, _, _ in _registered_repos()}
        return {slug for slug in resolved if slug}

    if forwarders is None and webhook_url is not None:
        forwarders = WebhookForwarders(
            slugs=forwarded_slugs,
            url=webhook_url,
            is_admin=is_admin,
            hooks=GithubHooks(requests),
            ledger=SettingsLedger(),
        )

    sync_slugs: dict[int, str | None] = {}

    def slug_of_repo(repo_id: int) -> str | None:
        """``owner/name`` of a registered repo's remote, from the database."""
        if repo_id not in sync_slugs:
            for registered_id, _, remote_url in _registered_repos():
                sync_slugs[registered_id] = repo_slug(remote_url)
        return sync_slugs.get(repo_id)

    slack: SlackLookup = NO_SLACK
    if slack_settings is not None:
        slack = SlackTracker(
            posts=_slack_posts,
            slug_of=slug_of_repo,
            bump_after=slack_settings.bump_after,
            permalink=slack_settings.permalink,
        )

    def webhook_problems() -> list[api.RepoWebhook]:
        """The repos the user administers whose webhook is not working."""
        if forwarders is None:
            return []
        problems = []
        for repo_id, _, remote_url in _registered_repos():
            slug = repo_slugs.get(repo_id) or repo_slug(remote_url)
            problem = forwarders.problem(slug) if slug else None
            if problem is not None:
                problems.append(
                    api.RepoWebhook(
                        repo_id=repo_id, state=problem.state, problem=problem.text
                    )
                )
        return problems

    if forwarders is not None:
        forwarders.on_change = lambda: hub.publish(
            "webhooks",
            {
                "webhooks": [
                    problem.model_dump(mode="json") for problem in webhook_problems()
                ]
            },
        )

    def webhooks_live(repo_id: int) -> bool:
        slug = repo_slugs.get(repo_id)
        return forwarders is not None and slug is not None and forwarders.is_live(slug)

    refresher = StatusRefresher(
        branches=_tracked_branches,
        fetch=fetch,
        hub=hub,
        on_change=publish_status,
        clock=clock,
        interval=refresh_interval,
        settled_interval=settled_refresh_interval,
        concurrency=concurrency,
        tick=tick,
        wall_clock=wall_clock,
        branch_id=row_id,
        gate=(
            GithubChangeGate(PrGate(conditional_get), slug_of) if change_gate else None
        ),
        persist=lookup_cache,
        observe=transitions.observe if transitions else None,
        full_interval=full_refresh_interval,
        pushed=webhooks_live,
    )
    task_feed = TaskFeed(
        tasks=lambda: _linked_tasks(tracker()),
        cache=task_cache,
        hub=hub,
        tick=tick,
    )

    untracked_feed = UntrackedPrFeed(
        repos=_tracked_repos,
        search=search_open_prs,
        hub=hub,
        remote_of=remote_of,
        clock=clock,
        interval=untracked_interval,
        tick=tick,
    )

    def pr_finished(repo_id: int, branch: str) -> bool:
        fetched = refresher.get(repo_id, branch)
        return (
            fetched is not None
            and fetched.status is not None
            and fetched.status.pr in (PrState.MERGED, PrState.CLOSED)
        )

    aggregates = AggregateService(
        search=search_history,
        history=stored_history,
        remote_of=remote_of,
        finished=pr_finished,
        wall_clock=wall_clock,
    )

    # The default change signal needs a connection held for as long as the app
    # serves (``data_version`` is per connection), opened on the event loop's
    # thread, which is where the watcher reads it.
    held_store: list[StackStore] = []

    def held_data_version() -> int:
        if not held_store:
            held_store.append(StackStore(default_db_path()))
        return held_store[0].data_version()

    watcher = StructureWatcher(
        version=db_version or held_data_version, hub=hub, tick=tick
    )

    # Tracked branches, runs and settings live in the stack DB: when it
    # changes, what the cards and the Agents view show may have too.
    def reload_slack_posts() -> None:
        """Another process recorded a post (``toolbelt slack link``): read the
        posts again and update the cards of the PRs whose posts changed."""
        if not isinstance(slack, SlackTracker):
            return
        changed_prs = slack.reload()
        if not changed_prs:
            return
        affected: set[BranchKey] = set()
        for tracked in _tracked_branches():
            looked_up = refresher.get(tracked.repo_id, tracked.branch)
            if (
                looked_up is not None
                and looked_up.facts is not None
                and (slug_of_repo(tracked.repo_id), looked_up.facts.number)
                in changed_prs
            ):
                affected.add(tracked.key)
        if affected:
            publish_slack(affected)

    def on_database_change() -> None:
        views.notify("agents", "extras")
        reload_slack_posts()
        refresh_decisions()

    watcher.on_change = on_database_change

    def branch_next(repo_id: int, name: str) -> api.NextActionInfo | None:
        status = branch_status(repo_id, _all_parents(repo_id), name, wall_clock())
        return status.next if isinstance(status, api.LoadedStatus) else None

    def next_kind(repo_id: int, name: str) -> str | None:
        action = branch_next(repo_id, name)
        return action.kind if action else None

    def would_merge(repo_id: int, name: str) -> bool:
        """Its CI is running and, once that passed, Merge would be its next
        action: approved, no conflicts or comments, not a draft, and any parent
        PR merged (and no agent holds it)."""
        looked_up = refresher.get(repo_id, name)
        if (
            looked_up is None
            or looked_up.status is None
            or looked_up.status.ci is not CiState.RUNNING
            or claims.working(repo_id, name)
            or claims.autonomous(repo_id, name)
            or claims.deciding(repo_id, name)
        ):
            return False
        now = wall_clock()
        parent = parent_context(
            repo_id, _all_parents(repo_id), name, refresher, now, claims, slack
        )
        passed = dataclasses.replace(looked_up.status, ci=CiState.SUCCESS)
        return next_action(passed, parent).kind == MERGE

    async def land(repo_id: int, name: str) -> LandResult:
        """Merges the branch's PR when its next action is Merge: what the Merge
        button and the merge queue do. Raises ``LandRefused`` when it cannot."""
        parents = await asyncio.to_thread(_all_parents, repo_id)
        if name not in parents:
            raise LandRefused(404, "no such branch")
        status = branch_status(repo_id, parents, name, wall_clock())
        if (
            not isinstance(status, api.LoadedStatus)
            or status.next.kind != "merge"
            or status.pr_url is None
        ):
            raise LandRefused(
                409, "Nothing to merge: the branch's next action is not Merge."
            )

        def repo_path() -> Path | None:
            store = StackStore(default_db_path())
            try:
                return next((r.path for r in store.repos() if r.id == repo_id), None)
            finally:
                store.close()

        path = await asyncio.to_thread(repo_path)
        if path is None:
            raise LandRefused(404, "no such repo")
        outcome = await asyncio.to_thread(merger, path, status.pr_url)
        return LandResult(ok=outcome.ok, output=outcome.output)

    async def land_queued(repo_id: int, name: str) -> LandResult:
        try:
            return await land(repo_id, name)
        except LandRefused as refused:
            return LandResult(ok=False, output=refused.detail)

    merge_queue = MergeQueue(
        entries=_merge_queue_entries,
        add=_add_to_merge_queue,
        remove=_remove_from_merge_queue,
        branches=_tracked_branches,
        fetched=refresher.get,
        next_kind=next_kind,
        would_merge=would_merge,
        land=land_queued,
        wall_clock=wall_clock,
        changed=publish_slack,
        tick=merge_queue_tick,
    )

    slack_service = (
        SlackService(
            config=slack_settings,
            tracker=slack,
            send_message=(slack_proxy or SlackProxy()).send,
            branches=_tracked_branches,
            fetched=refresher.get,
            parents=_all_parents,
            slug_of=slug_of_repo,
            next_action=branch_next,
            posts=_slack_posts,
            save=_save_slack_posts,
            record_session=_record_slack_session,
            wall_clock=wall_clock,
            changed=publish_slack,
        )
        if slack_settings is not None and isinstance(slack, SlackTracker)
        else None
    )
    bump_watcher = (
        BumpWatcher(
            tracker=slack,
            branches=_tracked_branches,
            fetched=refresher.get,
            wall_clock=wall_clock,
            on_change=publish_slack,
        )
        if isinstance(slack, SlackTracker)
        else None
    )

    task_status_sync = TaskStatusSync(
        branches=_tracked_branches,
        fetched=refresher.get,
        linked=lambda repo_id, branch: _linked_tracker(tracker(), repo_id, branch),
        moved=_task_status_moved,
        record=_record_task_status_move,
        wall_clock=wall_clock,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        watcher.check()  # baseline: only later writes are changes
        _abandon_agent_runs(wall_clock())
        await refresher.restore()
        tasks = [
            asyncio.create_task(refresher.run()),
            asyncio.create_task(task_feed.run()),
            asyncio.create_task(claims.run()),
            asyncio.create_task(spend.run()),
            asyncio.create_task(dispatcher.run()),
            asyncio.create_task(untracked_feed.run()),
            asyncio.create_task(watcher.run()),
            asyncio.create_task(aggregates.run()),
            asyncio.create_task(views.run()),
        ]
        if forwarders is not None:
            tasks.append(asyncio.create_task(forwarders.run()))
        if bump_watcher is not None:
            tasks.append(asyncio.create_task(bump_watcher.run()))
        tasks.append(asyncio.create_task(merge_queue.run()))
        tasks.append(asyncio.create_task(task_status_sync.run()))
        try:
            yield
        finally:
            hub.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(review_sessions.close)
            for store in held_store:
                store.close()

    app = FastAPI(
        title="toolbelt",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.hub = hub
    app.state.refresher = refresher
    app.state.task_feed = task_feed
    app.state.claims_feed = claims
    app.state.untracked_feed = untracked_feed
    app.state.structure_watcher = watcher
    app.state.aggregates = aggregates
    app.state.spend_feed = spend
    app.state.dispatcher = dispatcher
    app.state.agent_runner = runner
    app.state.forwarders = forwarders
    app.state.slack = slack
    app.state.merge_queue = merge_queue
    app.state.github = requests
    app.state.views = views
    app.include_router(review_router(review_sessions))

    @app.get("/assets/{path:path}", include_in_schema=False, response_model=None)
    def asset(path: str) -> FileResponse:
        root = (client_dist / "assets").resolve()
        file = (root / path).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            raise HTTPException(status_code=404)
        # Content-hashed file names: cacheable for good.
        return FileResponse(
            file, headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    # The mark is linked from index.html; `/favicon.ico` is what browsers ask
    # for without looking at the page, so it serves the same SVG.
    @app.get("/favicon.svg", include_in_schema=False, response_model=None)
    @app.get("/favicon.ico", include_in_schema=False, response_model=None)
    def favicon() -> FileResponse:
        file = client_dist / "favicon.svg"
        if not file.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            file,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/", include_in_schema=False, response_model=None)
    def index() -> FileResponse | PlainTextResponse:
        page = client_dist / "index.html"
        if not page.is_file():
            return PlainTextResponse(
                "The web client is not built: run `pnpm install && pnpm build` "
                "in web/ (`tt ui` does this itself).",
                status_code=503,
            )
        # The page's assets are content-hashed; the shell must not be cached.
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    @app.get("/api/snapshot", response_model=api.Snapshot)
    async def snapshot() -> api.Snapshot:
        # The claims loop ticks about once a second; bring the claims up to
        # date first, so a page that asks for the snapshot before its stream
        # opens never sees them older than its own request (on the event
        # loop, where the feed lives; a change is published before the
        # version is read, so it is in this snapshot).
        claims.poll()
        # Questions asked by another process are read from the database here
        # too: its watcher only runs while a page is connected.
        await asyncio.to_thread(reload_decisions)
        # Read before the branches: an event published meanwhile then makes
        # the snapshot look stale to /api/events, never the other way round.
        version = hub.version
        return build_snapshot(
            cwd_root,
            tracker=tracker(),
            refresher=refresher,
            task_feed=task_feed,
            untracked=untracked_feed.get(),
            now=wall_clock(),
            version=version,
            claims=claims,
            extras=views.extras,
            agents=views.agents,
            spend=views.spend,
            webhooks=webhook_problems(),
            slack=slack,
            merge_queue=merge_queue,
        )

    @app.get("/api/aggregate", response_model=api.Aggregate)
    async def aggregate(
        weeks: int = Query(aggregate_view.DEFAULT_WEEKS),
        repo: str | None = Query(None),
        refresh: bool = Query(False),
    ) -> api.Aggregate:
        # Use the background-refreshed history; an early request can warm it.
        if weeks not in aggregate_view.WINDOWS:
            raise HTTPException(
                status_code=400,
                detail=f"weeks must be one of {list(aggregate_view.WINDOWS)}",
            )
        try:
            return await aggregates.get(weeks, repo or None, refresh=refresh)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/spend", response_model=api.Spend)
    async def spend_view(
        days: int = Query(spend_module.DEFAULT_BREAKDOWN_DAYS),
    ) -> api.Spend:
        if days not in spend_module.BREAKDOWN_DAYS:
            raise HTTPException(
                status_code=400,
                detail=f"days must be one of {list(spend_module.BREAKDOWN_DAYS)}",
            )
        return await asyncio.to_thread(spend.spend, days)

    @app.get("/api/agents", response_model=api.Agents)
    async def agents_view() -> api.Agents:
        def build() -> api.Agents:
            store = StackStore(default_db_path())
            try:
                enabled = store.get_setting(AGENTS_ENABLED_SETTING, "false") == "true"
                launching = store.get_setting(AGENTS_LAUNCH_SETTING, "false") == "true"
                return build_agents(
                    dispatcher,
                    store,
                    enabled=enabled,
                    launching=launching,
                    now=wall_clock(),
                )
            finally:
                store.close()

        return await asyncio.to_thread(build)

    @app.post("/api/merge", status_code=202)
    async def merge_branch(body: api.MergeRequest) -> dict[str, bool]:
        """Merge the branch's PR. It is merged in the background: the PR reads
        ``merging`` until done, and a failure is its merge-queue ``notice``."""
        repo_id, name = branch_ref(body.id)
        parents = await asyncio.to_thread(_all_parents, repo_id)
        if name not in parents:
            raise HTTPException(status_code=404, detail="no such branch")
        try:
            merge_queue.merge_now(repo_id, name)
        except MergeQueueRefused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from refused
        return {"merging": True}

    @app.post("/api/merge/queue", response_model=api.MergeQueueInfo)
    async def queue_merge(body: api.MergeQueueRequest) -> api.MergeQueueInfo:
        """Merge the branch's PR as soon as its CI has passed."""
        repo_id, name = branch_ref(body.id)
        parents = await asyncio.to_thread(_all_parents, repo_id)
        if name not in parents:
            raise HTTPException(status_code=404, detail="no such branch")
        try:
            return merge_queue.queue(repo_id, name)
        except MergeQueueRefused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from refused

    @app.post("/api/merge/unqueue", status_code=204)
    async def unqueue_merge(body: api.MergeQueueRequest) -> None:
        repo_id, name = branch_ref(body.id)
        merge_queue.cancel(repo_id, name)

    def branch_ref(row_id_: str) -> tuple[int, str]:
        repo_part, _, name = row_id_.partition(":")
        try:
            return int(repo_part), name
        except ValueError as error:
            raise HTTPException(status_code=404, detail="no such branch") from error

    def slack_on() -> SlackService:
        if slack_service is None:
            raise HTTPException(status_code=404, detail="Slack is not configured.")
        return slack_service

    def decision_info(decision: Decision) -> api.DecisionInfo:
        return api.DecisionInfo(
            id=decision.id,
            branch=decision.branch,
            proposer=decision.proposer,  # type: ignore[arg-type]
            session_id=decision.session_id,
            title=decision.title,
            points=[
                api.DecisionPointInfo(
                    id=point.id,
                    title=point.title,
                    context=point.context,
                    options=[
                        api.DecisionOptionInfo(**asdict(option))
                        for option in point.options
                    ],
                    recommendation=api.DecisionRecommendationInfo(
                        option_id=point.recommendation.option_id,
                        reasoning=point.recommendation.reasoning,
                    )
                    if point.recommendation
                    else None,
                )
                for point in decision.points
            ],
            status=decision.status,  # type: ignore[arg-type]
            answers=[api.DecisionAnswerInfo(**asdict(a)) for a in decision.answers],
            created_at=decision.created_at,
            answered_at=decision.answered_at,
            delivered_at=decision.delivered_at,
        )

    @app.get("/api/decisions", response_model=api.DecisionList)
    async def list_decisions(id: str) -> api.DecisionList:
        repo_id, name = branch_ref(id)
        found = await asyncio.to_thread(decision_book.all)
        return api.DecisionList(
            decisions=[
                decision_info(d)
                for d in found
                if (d.repo_id, d.branch) == (repo_id, name)
            ]
        )

    @app.post("/api/decisions/answer", response_model=api.DecisionInfo)
    async def answer_decision(body: api.DecisionAnswerRequest) -> api.DecisionInfo:
        try:
            answered = await asyncio.to_thread(
                decision_book.answer,
                body.decision_id,
                [a.model_dump() for a in body.answers],
            )
        except NoSuchDecision as error:
            raise HTTPException(status_code=404, detail="no such decision") from error
        except AlreadyAnswered as error:
            raise HTTPException(
                status_code=409, detail="This decision was answered already."
            ) from error
        except InvalidDecision as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        refresh_decisions()
        return decision_info(answered)

    @app.post("/api/decisions/delete", response_model=api.DecisionInfo)
    async def delete_decision(body: api.DecisionDeleteRequest) -> api.DecisionInfo:
        try:
            deleted = await asyncio.to_thread(decision_book.delete, body.decision_id)
        except NoSuchDecision as error:
            raise HTTPException(status_code=404, detail="no such decision") from error
        refresh_decisions()
        return decision_info(deleted)

    @app.post("/api/slack/draft", response_model=api.SlackDraft)
    async def slack_draft(body: api.SlackDraftRequest) -> api.SlackDraft:
        service = slack_on()
        repo_id, name = branch_ref(body.id)
        parents = await asyncio.to_thread(_all_parents, repo_id)
        if name not in parents:
            raise HTTPException(status_code=404, detail="no such branch")
        try:
            return await asyncio.to_thread(
                service.draft,
                repo_id,
                name,
                body.assignee_id,
                body.prs,
                body.reassign,
            )
        except SlackNotApplicable as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/slack/send", response_model=api.SlackAccepted, status_code=202)
    async def slack_send(body: api.SlackSendRequest) -> api.SlackAccepted:
        service = slack_on()
        try:
            return await service.send(body)
        except SlackNotApplicable as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SlackUnknown as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/agents/enabled", response_model=api.AgentsEnabled)
    async def set_agents_enabled(body: api.AgentsEnabled) -> api.AgentsEnabled:
        await asyncio.to_thread(
            _write_setting, AGENTS_ENABLED_SETTING, "true" if body.enabled else "false"
        )
        views.notify("agents")
        return body

    @app.post("/api/agents/launching", response_model=api.AgentsLaunching)
    async def set_agents_launching(
        body: api.AgentsLaunching,
    ) -> api.AgentsLaunching:
        await asyncio.to_thread(
            _write_setting,
            AGENTS_LAUNCH_SETTING,
            "true" if body.launching else "false",
        )
        views.notify("agents")
        return body

    @app.post("/api/agents/runs/{run_id}/stop")
    async def stop_agent_run(run_id: str) -> dict[str, bool]:
        if not runner.stop(run_id):
            raise HTTPException(status_code=404, detail="no such run is working")
        return {"stopping": True}

    @app.get(
        "/api/agents/runs/{run_id}/conversation", response_model=api.AgentConversation
    )
    async def agent_conversation(run_id: str) -> api.AgentConversation:
        conversation = await asyncio.to_thread(
            conversation_of,
            run_id,
            db_path=default_db_path(),
            projects=transcripts_dir(),
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="no such run")
        return conversation

    @app.post("/api/agents/start", status_code=202)
    async def start_agent_run(body: api.AgentStart) -> dict[str, bool]:
        """Start a run on a task by hand. The run is started in the background:
        the task reads ``starting`` until it is recorded, and a refusal is
        reported on the task."""
        repo_id, _, branch = body.id.partition(":")
        try:
            dispatcher.request_start((int(repo_id), branch))
        except (KeyError, ValueError) as error:
            raise HTTPException(
                status_code=404, detail="no such task for an agent"
            ) from error
        return {"starting": True}

    # Webhooks received since the server started, by event, and how many of
    # them named a tracked branch.
    webhook_counts = {"received": 0, "hinted": 0}

    @app.get("/api/github-requests")
    async def github_requests() -> dict:
        """Each GitHub budget as last reported, and the requests each caller
        has made since the server started."""
        return {**requests.usage(), "webhooks": webhook_counts}

    @app.post("/api/github-webhook", status_code=204)
    async def github_webhook(request: Request) -> Response:
        # A hint only (see github_webhooks.py): refetch the branches it names.
        hit = affected(request.headers.get("x-github-event", ""), await request.json())
        webhook_counts["received"] += 1
        if hit is not None:
            keys = set()
            for branch in _tracked_branches():
                if await slug_of(branch.repo_id) != hit.repo:
                    continue
                fetched = refresher.get(branch.repo_id, branch.branch)
                number = fetched.facts.number if fetched and fetched.facts else None
                if branch.branch in hit.branches or number in hit.numbers:
                    keys.add(branch.key)
            webhook_counts["hinted"] += len(keys)
            refresher.hint(keys)
        return Response(status_code=204)

    @app.post("/api/refresh", response_model=api.RefreshAccepted, status_code=202)
    async def refresh_branch(body: api.RefreshRequest) -> api.RefreshAccepted:
        repo_id, name = branch_ref(body.id)
        if not refresher.refresh(repo_id, name):
            raise HTTPException(status_code=404, detail="no such branch")
        return api.RefreshAccepted(refreshing=True)

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            queue = hub.subscribe()
            try:
                yield _sse(
                    "hello",
                    {
                        "v": hub.version,
                        "age": refresher.seconds_since_refresh(),
                        "build": served_build(client_dist),
                    },
                )
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), keepalive_seconds)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        return
                    yield _sse(*item)
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
