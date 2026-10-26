"""`/api/proposals` through the app: the list, approving and rejecting."""

import pytest
from fastapi.testclient import TestClient
from test_proposals import content_data

from toolbelt.proposals.book import ProposalBook
from toolbelt.proposals.model import content_from_data
from toolbelt.ui.app import create_app
from toolbelt.ui.pr_fetch import PrSnapshot


async def no_pr(branch: str, root) -> PrSnapshot:
    raise RuntimeError("no network in this test")


@pytest.fixture
def client():
    book = ProposalBook()
    book.propose(
        [
            (
                "jira",
                f"TB-{n}",
                f"https://jira/TB-{n}",
                content_from_data(content_data(f"t{n}")),
            )
            for n in range(2)
        ],
        run_id=None,
    )
    with TestClient(create_app(fetch=no_pr, tick=0.05)) as client:
        yield client


def first_id(client) -> str:
    return client.get("/api/proposals").json()["proposals"][0]["id"]


def test_lists_the_proposals_with_their_content_and_the_cap(client):
    body = client.get("/api/proposals").json()

    assert body["max_pending"] == 5
    assert [p["status"] for p in body["proposals"]] == ["pending", "pending"]
    assert body["proposals"][0]["content"]["why_now"] == "Fired 31 times yesterday."


def test_rejecting_records_the_reason_and_keeps_the_proposal(client):
    proposal_id = first_id(client)

    response = client.post(
        "/api/proposals/reject",
        json={"proposal_id": proposal_id, "reason": "too big", "tags": ["too_big"]},
    )

    assert response.status_code == 200
    assert response.json()["decision_reason"] == "too big"
    listed = client.get("/api/proposals").json()["proposals"]
    assert sorted(p["status"] for p in listed) == ["pending", "rejected"]


def test_a_proposal_cannot_be_decided_twice(client):
    proposal_id = first_id(client)
    client.post("/api/proposals/approve", json={"proposal_id": proposal_id})

    again = client.post("/api/proposals/approve", json={"proposal_id": proposal_id})

    assert again.status_code == 409


def test_deciding_an_unknown_proposal_is_not_found(client):
    response = client.post("/api/proposals/approve", json={"proposal_id": "nope"})

    assert response.status_code == 404


def test_the_user_can_write_guidelines_and_the_list_returns_them(client):
    response = client.post(
        "/api/proposals/guidelines", json={"body": "- Prefer small fixes"}
    )

    assert response.status_code == 200
    assert response.json()["guidelines"]["body"] == "- Prefer small fixes"
    assert response.json()["guidelines"]["author"] == "user"
