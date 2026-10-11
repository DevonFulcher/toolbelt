"""The state catalog: every state a card shows has a timeline representation."""

import itertools
from typing import get_args

import pytest

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.ui import api_models, pr_activity
from toolbelt.ui import next_action as na
from toolbelt.ui import pr_states
from toolbelt.ui.pr_states import CHIPS, EVENTS, NOW_STATE, PHASES, SLOT_STATES, STATES


def dimension_values(dimension: str) -> set[str]:
    return {s.value for s in STATES if s.dimension == dimension}


@pytest.mark.parametrize(
    ("dimension", "enum"),
    [("pr", PrState), ("ci", CiState), ("review", ReviewState)],
)
def test_every_enum_value_has_a_catalog_entry_with_a_representation(dimension, enum):
    assert dimension_values(dimension) == {member.value for member in enum}
    for member in enum:
        state = pr_states.lookup(dimension, member.value)
        assert state.reps, member
        assert state.label
        assert state.legend
        assert state.tone


def test_every_representation_names_something_the_renderer_can_draw():
    for state in STATES:
        for rep in state.reps:
            known = {"event": EVENTS, "phase": PHASES, "now_state": CHIPS}[rep.how]
            assert rep.kind in known, (state.dimension, state.value, rep)


def test_the_core_dimensions_are_drawn_as_now_state_chips():
    chipped = {s.dimension for s in STATES if any(r.how == NOW_STATE for r in s.reps)}
    assert {"pr", "ci", "review", "comments", "merge", "next"} <= chipped
    assert set(CHIPS) <= chipped


def all_statuses():
    for pr, ci, review, conflicts, comments in itertools.product(
        PrState, CiState, ReviewState, (False, True), (0, 2)
    ):
        yield BranchStatus(
            pr=pr,
            ci=ci,
            review=review,
            url=None,
            open_comments=comments,
            has_conflicts=conflicts,
        )


def test_next_action_kinds_are_exactly_the_catalogued_ones():
    produced = set()
    for status in all_statuses():
        for parent in (
            None,
            na.ParentContext("p", PrState.OPEN, na.WHO_YOU),
            na.ParentContext("p", PrState.OPEN, na.WHO_CI),
            na.ParentContext("p", PrState.MERGED),
        ):
            produced.add(na.next_action(status, parent).kind)

    assert produced == set(na.KINDS)
    assert dimension_values("next") == set(na.KINDS)


def test_current_states_look_every_value_up_in_the_catalog():
    for status in all_statuses():
        action = na.next_action(status, None)
        merge = "none"
        if status.pr in (PrState.OPEN, PrState.DRAFT):
            merge = "conflicts" if status.has_conflicts else "clean"
        states = pr_states.current_states(
            pr=status.pr.value,
            ci=status.ci.value,
            review=status.review.value,
            open_comments=status.open_comments,
            merge=merge,
            next_kind=action.kind,
            next_who=action.who,
            next_label=action.label,
        )
        for state in states:
            assert pr_states.lookup(state["dimension"], state["value"])
        # The next action is always a chip; a PR-less branch has only PR + Next.
        assert states[-1]["dimension"] == "next"
        if status.pr is PrState.NONE:
            assert [s["dimension"] for s in states] == ["pr", "next"]


def test_an_unknown_state_value_is_an_error_not_a_blank():
    with pytest.raises(KeyError):
        pr_states.current_states(
            pr="open",
            ci="exploded",
            review="none",
            open_comments=0,
            merge="clean",
            next_kind="merge",
            next_who="you",
            next_label="Merge",
        )


def test_slot_states_map_onto_catalog_dimensions():
    dimensions = {s.dimension for s in STATES}
    for slot, dimension in SLOT_STATES.items():
        assert dimension is None or dimension in dimensions, slot


def test_the_catalog_is_the_wire_model_the_client_is_generated_from():
    catalog = api_models.build_catalog()

    assert catalog.events.review_approved.glyph == "✓"
    assert {s.dimension for s in catalog.states} >= {"pr", "ci", "review"}
    # The wire model validated every value (an unknown one would raise), and
    # lists exactly the catalog's states and slots.
    assert [(s.dimension, s.value) for s in catalog.states] == [
        (s.dimension, s.value) for s in STATES
    ]
    assert catalog.slots.model_dump() == SLOT_STATES


@pytest.mark.parametrize(
    ("alias", "keys"),
    [
        (pr_states.EventKind, EVENTS),
        (pr_states.PhaseKind, PHASES),
        (pr_states.ChipDimension, CHIPS),
    ],
)
def test_the_literal_types_list_exactly_the_catalog_keys(alias, keys):
    assert set(get_args(alias.__value__)) == set(keys)


def test_the_wire_literals_match_the_rules_they_describe():
    assert set(get_args(na.NextKind.__value__)) == set(na.KINDS)
    assert set(get_args(na.Who.__value__)) == set(na.WHO_ORDER)
    kinds = {
        value
        for name, value in vars(pr_activity).items()
        if name.isupper() and isinstance(value, str)
    }
    assert set(get_args(pr_activity.ActivityKind.__value__)) == kinds


def test_the_task_start_marker_and_working_phase_are_represented():
    event_kinds = {r.kind for s in STATES for r in s.reps if r.how == "event"}
    phase_kinds = {r.kind for s in STATES for r in s.reps if r.how == "phase"}

    assert "started" in event_kinds
    assert set(PHASES) == phase_kinds


def test_the_task_start_is_a_catalogued_marker_and_working_phase():
    started = pr_states.lookup("task", "started")
    unknown = pr_states.lookup("task", "unknown")

    assert {(r.how, r.kind, r.absent) for r in started.reps} == {
        ("event", "started", False),
        ("phase", "working", False),
    }
    # An unknown start says so: nothing is drawn before the PR opened.
    assert all(r.absent for r in unknown.reps)
    assert EVENTS["started"].label == "Task started"
    assert PHASES["working"].label == "Working (no PR yet)"
    assert SLOT_STATES["task_age"] == "task"
