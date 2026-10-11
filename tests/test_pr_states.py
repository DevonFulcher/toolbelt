"""The state catalog: every state a card shows has a timeline representation."""

import itertools
import json

import pytest

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
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


def test_the_catalog_serializes_to_json_the_page_embeds():
    data = json.loads(pr_states.catalog_json())

    assert set(data) == {"events", "phases", "chips", "states", "not_represented"}
    assert data["events"]["review_approved"]["glyph"] == "✓"
    assert {s["dimension"] for s in data["states"]} >= {"pr", "ci", "review"}
