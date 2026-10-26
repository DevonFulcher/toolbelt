from datetime import UTC, datetime
from pathlib import Path

import pytest

from toolbelt.git.stack.store import StackStore
from toolbelt.proposals.book import AlreadyDecided, ProposalBook
from toolbelt.proposals.model import InvalidProposal, content_from_data

SRC = Path(__file__).parent.parent / "src" / "toolbelt"


def content_data(title: str = "Retry Jira fetch on 429") -> dict:
    return {
        "title": title,
        "summary": "The feed gives up on the first rate limit.",
        "problem": "Cards show stale titles until the next full refresh.",
        "why_now": "Fired 31 times yesterday.",
        "evidence": [{"quote": "429 from Jira", "url": "https://example.com/1"}],
        "approach": "Wrap the fetch in a bounded retry with backoff.",
        "acceptance_criteria": ["A 429 is retried up to 3 times"],
        "risks": ["Hides a real outage for a few seconds"],
        "size": "S",
        "confidence": "high",
        "repo": "toolbelt",
        "related": [],
        "on_approve": "Implement on a new branch and stop for your review.",
    }


def make_book(tmp_path: Path, max_pending: int = 5) -> ProposalBook:
    return ProposalBook(
        open_store=lambda: StackStore(tmp_path / "stacks.db"),
        wall_clock=lambda: datetime(2026, 10, 25, 12, 0, tzinfo=UTC),
        max_pending=max_pending,
    )


def items(count: int, start: int = 0):
    return [
        (
            "jira",
            f"TB-{n}",
            f"https://jira/TB-{n}",
            content_from_data(content_data(f"t{n}")),
        )
        for n in range(start, start + count)
    ]


def test_only_as_many_proposals_are_stored_as_there_are_free_slots(tmp_path):
    book = make_book(tmp_path)

    stored = book.propose(items(8), run_id="r1")

    assert len(stored) == 5
    assert len(book.pending()) == 5


def test_deciding_a_proposal_frees_a_slot(tmp_path):
    book = make_book(tmp_path)
    first = book.propose(items(5), run_id="r1")
    book.reject(first[0].id, reason="not now", tags=["low_value"])

    stored = book.propose(items(3, start=5), run_id="r2")

    assert len(stored) == 1


def test_a_source_item_is_proposed_once_even_after_it_was_rejected(tmp_path):
    book = make_book(tmp_path)
    first = book.propose(items(1), run_id="r1")
    book.reject(first[0].id)

    assert book.propose(items(1), run_id="r2") == []


def test_rejecting_records_the_reason_and_tags_and_keeps_the_proposal(tmp_path):
    book = make_book(tmp_path)
    proposal = book.propose(items(1), run_id="r1")[0]

    rejected = book.reject(
        proposal.id, reason="  too big for an agent ", tags=["too_big"]
    )

    assert rejected.status == "rejected"
    assert rejected.decision_reason == "too big for an agent"
    assert rejected.decision_tags == ("too_big",)
    assert rejected.decided_at is not None
    assert [p.id for p in book.all()] == [proposal.id]


def test_a_decided_proposal_cannot_be_decided_again(tmp_path):
    book = make_book(tmp_path)
    proposal = book.propose(items(1), run_id="r1")[0]
    book.approve(proposal.id)

    with pytest.raises(AlreadyDecided):
        book.reject(proposal.id)


def test_an_unknown_tag_is_refused(tmp_path):
    book = make_book(tmp_path)
    proposal = book.propose(items(1), run_id="r1")[0]

    with pytest.raises(InvalidProposal):
        book.reject(proposal.id, tags=["nope"])


def test_content_must_be_complete():
    data = content_data()
    del data["why_now"]

    with pytest.raises(InvalidProposal):
        content_from_data(data)


def test_nothing_deletes_proposals():
    sources = [path for path in SRC.rglob("*.py")]
    offenders = [
        str(path)
        for path in sources
        if "DELETE FROM proposals" in path.read_text()
        or "DROP TABLE proposals" in path.read_text()
    ]

    assert offenders == []
