from datetime import UTC, datetime

import pytest

from toolbelt.software_factory.state import FactoryState, WrongStatus


def make_state() -> FactoryState:
    return FactoryState(lambda: datetime(2026, 10, 25, 12, 0, tzinfo=UTC))


def test_work_item_flows_from_proposed_to_pr_opened():
    state = make_state()
    run = state.approve_work_item("wi-101")
    while state.step_run(run.id):
        pass

    item = next(w for w in state.work_items if w.id == "wi-101")
    assert item.status == "changeset_ready"
    assert item.changeset_id is not None

    state.approve_changeset(item.changeset_id)

    assert item.status == "pr_opened"
    assert item.pr_url is not None


def test_rejecting_a_changeset_rejects_its_work_item():
    state = make_state()
    state.reject_changeset("cs-1")

    item = next(w for w in state.work_items if w.id == "wi-107")
    assert item.status == "rejected"


def test_a_work_item_cannot_be_approved_twice():
    state = make_state()
    state.approve_work_item("wi-101")

    with pytest.raises(WrongStatus):
        state.approve_work_item("wi-101")
