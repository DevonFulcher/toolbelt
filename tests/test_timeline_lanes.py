"""Every status the card shows has a place on its timeline (see
``timeline_lanes.py``): a lane draws it, the PR track does, or
``NOT_TIMELINE_STATUS`` says why not."""

import typing
from datetime import UTC, datetime, timedelta
from enum import Enum

from toolbelt.git.stack.store import RepoIdentity, StackStore, default_db_path
from toolbelt.ui import api_models, pr_states
from toolbelt.ui.lane_history import build_lanes
from toolbelt.ui.timeline_lanes import (
    LANE_EVENTS,
    LANE_PHASES,
    LANE_SOURCES,
    NOT_TIMELINE_STATUS,
)

#: Status types the PR track draws (``pr_states``).
PR_TRACK_STATUS = {
    "PrState",
    "CiState",
    "ReviewState",
    "CommentsValue",
    "MergeValue",
    "NextKind",
    "ActivityKind",
    "EventKind",
    "PhaseKind",
    "ChipDimension",
    "CatalogDimension",
}
#: Types of the lanes' own vocabulary and plumbing.
LANE_PLUMBING = {"LaneId", "LaneEventKind", "LanePhaseKind"}


def values(status: object) -> set[str]:
    if isinstance(status, type) and issubclass(status, Enum):
        return {str(member.value) for member in status}
    return {str(v) for v in typing.get_args(status.__value__)}  # type: ignore[attr-defined]


def status_types() -> dict[str, object]:
    """Every enum and ``Literal`` alias of ``api_models``'s namespace."""
    found: dict[str, object] = {}
    for name, obj in vars(api_models).items():
        if (
            (
                isinstance(obj, typing.TypeAliasType)
                and typing.get_origin(obj.__value__) is typing.Literal
            )
            or isinstance(obj, type)
            and issubclass(obj, Enum)
            and obj is not Enum
        ):
            found[name] = obj
    return found


def test_every_status_type_is_drawn_or_says_why_not():
    covered = {c.status for source in LANE_SOURCES for c in source.covers}
    for name in status_types():
        assert (
            name in covered
            or name in PR_TRACK_STATUS
            or name in LANE_PLUMBING
            or name in NOT_TIMELINE_STATUS
        ), f"{name} is a status with no timeline lane: cover it in timeline_lanes.py"


def test_every_value_of_a_covered_type_maps_to_kinds_of_its_lane():
    types = status_types()
    for source in LANE_SOURCES:
        for coverage in source.covers:
            if coverage.detail:
                continue
            status = types[coverage.status]
            assert set(coverage.kinds) == values(status), (source.id, coverage.status)
            for kinds in coverage.kinds.values():
                for kind in kinds:
                    assert kind in source.events or kind in source.phases, (
                        source.id,
                        kind,
                    )


def test_every_kind_is_used_by_a_covered_value_or_its_source_history():
    # The merge queue has no status type: its single span is drawn from the
    # queue itself.
    used = {
        kind
        for source in LANE_SOURCES
        for coverage in source.covers
        for kinds in coverage.kinds.values()
        for kind in kinds
    } | {"merge_queued"}
    # Claims cross HolderKind with ClaimKind, so each holder maps to both.
    assert used == set(LANE_EVENTS) | set(LANE_PHASES)


def test_the_not_drawn_list_names_real_types_and_nothing_covered():
    covered = {c.status for source in LANE_SOURCES for c in source.covers}
    for name in NOT_TIMELINE_STATUS:
        assert name in status_types(), name
        assert name not in covered, name


def test_the_catalog_carries_every_lane_kind():
    catalog = api_models.build_catalog()
    assert [lane.id for lane in catalog.lanes] == [s.id for s in LANE_SOURCES]
    assert set(catalog.lane_events.model_dump()) == set(LANE_EVENTS)
    assert set(catalog.lane_phases.model_dump()) == set(LANE_PHASES)
    assert pr_states.EVENTS  # the PR track keeps its own vocabulary


def test_a_branchs_history_reaches_its_lanes():
    store = StackStore(default_db_path())
    repo_id = store.ensure_repo(
        RepoIdentity(
            git_common_dir=default_db_path().parent / "x" / ".git",
            path=default_db_path().parent / "x",
            remote_url=None,
        )
    )
    store.set_parent(repo_id, "a", "main")
    (facts,) = store.tracked_branch_facts()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    store.record_status_transition(repo_id, "a", "conflict", "clean", t0)
    store.record_status_transition(
        repo_id, "a", "conflict", "conflicts", t0 + timedelta(hours=1)
    )
    store.record_status_transition(
        repo_id, "a", "conflict", "conflicts", t0 + timedelta(hours=2)
    )
    store.record_status_transition(
        repo_id, "a", "conflict", "clean", t0 + timedelta(hours=3)
    )
    store.record_claim_seen(
        holder="h",
        session_id=None,
        agent_id=None,
        holder_kind="agent",
        claim_kind="worktree",
        target="/w",
        task_id=facts.task_id,
        touched_at=t0,
        seen_at=t0 + timedelta(minutes=5),
        max_gap=timedelta(seconds=30),
    )
    lanes = build_lanes(store, t0 + timedelta(days=1), lambda _: "o/x")[(repo_id, "a")]

    by_lane = {lane.lane: lane for lane in lanes}
    assert list(by_lane) == ["git", "claim"]
    assert [(p.kind, p.start, p.end) for p in by_lane["git"].phases] == [
        ("git_conflicting", "2026-01-01T01:00:00Z", "2026-01-01T03:00:00Z")
    ]
    (claim,) = by_lane["claim"].phases
    assert (claim.kind, claim.end) == ("claim_agent_worktree", "2026-01-01T00:05:00Z")


def test_a_lookup_records_conflict_and_ci_once_per_change():
    from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
    from toolbelt.ui.status_transitions import StatusTransitionRecorder

    def status(conflicts: bool, ci: CiState) -> BranchStatus:
        return BranchStatus(
            pr=PrState.OPEN,
            ci=ci,
            review=ReviewState.NONE,
            has_conflicts=conflicts,
            url="u",
            open_comments=0,
            bot_comments=0,
        )

    recorder = StatusTransitionRecorder()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    assert recorder.observe(1, "a", status(False, CiState.RUNNING), at)
    assert not recorder.observe(1, "a", status(False, CiState.RUNNING), at)
    assert recorder.observe(1, "a", status(True, CiState.RUNNING), at)
    store = StackStore(default_db_path())
    assert [(t.dimension, t.value) for t in store.status_transitions()] == [
        ("conflict", "clean"),
        ("ci", "running"),
        ("conflict", "conflicts"),
    ]
