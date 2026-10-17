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
import functools
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from toolbelt.autonomous_agents.auth import claude_signed_in
from toolbelt.autonomous_agents.branch_state import inspect_branch
from toolbelt.autonomous_agents.conversation import find_transcript, read_conversation
from toolbelt.autonomous_agents.dispatcher import (
    Dispatcher,
    DispatchTask,
    OverrideRefused,
)
from toolbelt.autonomous_agents.limits import (
    AGENTS_ENABLED_SETTING,
    AGENTS_LAUNCH_SETTING,
    load_limits,
)
from toolbelt.autonomous_agents.runner import AgentRunner
from toolbelt.autonomous_agents.screen import read_screen_state
from toolbelt.git.repo_pull import default_config_path
from toolbelt.git.stack.status import PrState
from toolbelt.git.stack.store import (
    AgentRun,
    Parents,
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
    gh_page,
    read_history,
)
from toolbelt.ui.card_extras import build_card_extras
from toolbelt.ui.claims_feed import (
    CLAIMS_TICK_SECONDS,
    SPAN_MAX_GAP,
    BranchKey,
    ClaimObservation,
    ClaimsFeed,
)
from toolbelt.ui.client_build import CLIENT_DIST
from toolbelt.ui.conflict_commit import conflict_for
from toolbelt.ui.live_status import (
    DEFAULT_CONCURRENCY,
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
from toolbelt.ui.merge_pr import MergeOutcome, run_toolbelt_merge
from toolbelt.ui.pr_fetch import PrSnapshot, fetch_snapshot
from toolbelt.ui.snapshot import (
    branch_claim,
    build_snapshot,
    descendants,
    live_status,
    parent_context,
    row_id,
)
from toolbelt.ui.task_cache import TaskInfoCache
from toolbelt.ui.task_feed import LinkedTask, TaskFeed
from toolbelt.ui.untracked_prs import (
    DEFAULT_INTERVAL_SECONDS as DEFAULT_UNTRACKED_INTERVAL_SECONDS,
)
from toolbelt.ui.untracked_prs import (
    RemoteLookup,
    SearchRunner,
    TrackedRepo,
    UntrackedPrFeed,
    gh_search,
    origin_remote,
)

# Sent when no event has gone out for this long, so intermediaries keep the
# stream open and a dead client is noticed.
_KEEPALIVE_SECONDS = 15.0


async def fetch_status(branch: str, root: Path) -> PrSnapshot:
    snapshot = await fetch_snapshot(branch, root)
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
            for repo in store.repos()
            if repo.path.is_dir()
            for name in store.all_parents(repo.id)
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
            for repo in store.repos()
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
        for repo in store.repos():
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
        paths = {repo.id: repo.path for repo in store.repos()}
        return [
            DispatchTask(task=task, repo_path=paths[task.repo_id])
            for task in tracked_tasks(store, git_worktrees)
            if paths[task.repo_id].is_dir()
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
    fetch: StatusFetcher = fetch_status,
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
    search_open_prs: SearchRunner = gh_search,
    remote_of: RemoteLookup = origin_remote,
    untracked_interval: float = DEFAULT_UNTRACKED_INTERVAL_SECONDS,
    search_history: PageRunner = gh_page,
    stored_history: Callable[[], StoredHistory] = read_history,
    claims_dir: Callable[[], Path] = _claims_dir,
    claim_tasks: Callable[[], list[TrackedTask]] = _claim_tasks,
    record_claim: Callable[[ClaimObservation], None] = _record_claim,
    claims_tick: float | None = None,
    transcripts_dir: Callable[[], Path] = projects_dir,
    spend_interval: float = spend_module.DEFAULT_INTERVAL_SECONDS,
    dispatcher: Dispatcher | None = None,
    merger: Callable[[Path, str], MergeOutcome] = run_toolbelt_merge,
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
    and sign-in; it only decides, see ``autonomous_agents``)."""
    task_cache = TaskInfoCache(ttl_seconds=task_ttl_seconds, clock=clock)
    hub = EventHub(clock=clock, grace=grace_seconds)

    def branch_status(repo_id: int, parents: Parents, name: str, now: datetime):
        return live_status(
            refresher.get(repo_id, name),
            parent_context(repo_id, parents, name, refresher, claims),
            now,
            claims.working(repo_id, name),
            claims.autonomous(repo_id, name),
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

    def publish_status(branch: TrackedBranch, fetched: Fetched) -> None:
        # The cache already holds ``fetched``, so parent contexts see it.
        parents = _all_parents(branch.repo_id)
        now = wall_clock()
        for name in descendants(parents, branch.branch):
            status = branch_status(branch.repo_id, parents, name, now)
            hub.publish(
                "status",
                {
                    "branch_id": row_id(branch.repo_id, name),
                    "status": status.model_dump(mode="json"),
                },
            )

    claims = ClaimsFeed(
        directory=claims_dir,
        tasks=claim_tasks,
        on_change=publish_claims,
        record=record_claim,
        wall_clock=wall_clock,
        tick=min(tick, CLAIMS_TICK_SECONDS) if claims_tick is None else claims_tick,
    )

    spend = spend_module.SpendFeed(
        db_path=default_db_path,
        projects=transcripts_dir,
        wall_clock=wall_clock,
        interval=spend_interval,
    )

    limits = load_limits(default_config_path())
    runner = AgentRunner(
        db_path=default_db_path,
        claims_dir=claims_dir,
        limits=limits,
        wall_clock=wall_clock,
    )
    if dispatcher is None:
        dispatcher = Dispatcher(
            tasks=_dispatch_tasks,
            fetch=fetch,
            holders=claims.holders,
            inspect=lambda item: inspect_branch(
                item.repo_path, item.task.branch, item.task.worktree
            ),
            runs=lambda: _agent_runs(wall_clock()),
            enabled=_agents_enabled,
            limits=limits,
            screen=read_screen_state,
            signed_in=claude_signed_in,
            wall_clock=wall_clock,
            launch=lambda decision: _start_run(runner, decision),
            launching=_agents_launching,
        )

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

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        watcher.check()  # baseline: only later writes are changes
        _abandon_agent_runs(wall_clock())
        tasks = [
            asyncio.create_task(refresher.run()),
            asyncio.create_task(task_feed.run()),
            asyncio.create_task(claims.run()),
            asyncio.create_task(spend.run()),
            asyncio.create_task(dispatcher.run()),
            asyncio.create_task(untracked_feed.run()),
            asyncio.create_task(watcher.run()),
            asyncio.create_task(aggregates.run()),
        ]
        try:
            yield
        finally:
            hub.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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

    @app.get("/api/card-extras", response_model=api.CardExtras)
    async def card_extras() -> api.CardExtras:
        def build() -> api.CardExtras:
            store = StackStore(default_db_path())
            try:
                return build_card_extras(dispatcher, store, wall_clock())
            finally:
                store.close()

        return await asyncio.to_thread(build)

    @app.post("/api/merge", response_model=api.MergeResult)
    async def merge_branch(body: api.MergeRequest) -> api.MergeResult:
        repo_part, _, branch = body.id.partition(":")
        try:
            repo_id = int(repo_part)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="no such branch") from error
        parents = await asyncio.to_thread(_all_parents, repo_id)
        if branch not in parents:
            raise HTTPException(status_code=404, detail="no such branch")
        status = branch_status(repo_id, parents, branch, wall_clock())
        if (
            not isinstance(status, api.LoadedStatus)
            or status.next.kind != "merge"
            or status.pr_url is None
        ):
            raise HTTPException(
                status_code=409,
                detail="Nothing to merge: the branch's next action is not Merge.",
            )

        def repo_path() -> Path | None:
            store = StackStore(default_db_path())
            try:
                return next((r.path for r in store.repos() if r.id == repo_id), None)
            finally:
                store.close()

        path = await asyncio.to_thread(repo_path)
        if path is None:
            raise HTTPException(status_code=404, detail="no such repo")
        outcome = await asyncio.to_thread(merger, path, status.pr_url)
        return api.MergeResult(ok=outcome.ok, output=outcome.output)

    @app.post("/api/agents/enabled", response_model=api.AgentsEnabled)
    async def set_agents_enabled(body: api.AgentsEnabled) -> api.AgentsEnabled:
        await asyncio.to_thread(
            _write_setting, AGENTS_ENABLED_SETTING, "true" if body.enabled else "false"
        )
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
        def read() -> api.AgentConversation:
            store = StackStore(default_db_path())
            try:
                run = next((r for r in store.agent_runs() if r.run_id == run_id), None)
            finally:
                store.close()
            if run is None:
                raise HTTPException(status_code=404, detail="no such run")
            transcript = (
                find_transcript(run.session_id, projects_dir())
                if run.session_id
                else None
            )
            entries = read_conversation(transcript) if transcript else []
            return api.AgentConversation(
                run_id=run_id,
                running=run.state == "running",
                messages=[
                    api.AgentMessage(
                        kind=e.kind,
                        at=e.at,
                        text=e.text,
                        tool=e.tool,
                        is_error=e.is_error,
                    )
                    for e in entries
                ],
            )

        return await asyncio.to_thread(read)

    @app.post("/api/agents/start")
    async def start_agent_run(body: api.AgentStart) -> dict[str, bool]:
        repo_id, _, branch = body.id.partition(":")
        try:
            await dispatcher.start_now((int(repo_id), branch))
        except (KeyError, ValueError) as error:
            raise HTTPException(
                status_code=404, detail="no such task for an agent"
            ) from error
        except OverrideRefused as refused:
            raise HTTPException(
                status_code=409, detail=refused.reason.text
            ) from refused
        return {"starting": True}

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            queue = hub.subscribe()
            try:
                yield _sse(
                    "hello",
                    {"v": hub.version, "age": refresher.seconds_since_refresh()},
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
