"""Storing, listing and deciding proposals.

Everything that touches a proposal goes through ``ProposalBook``: the proposer
that writes them, the web UI that decides them and the worker that picks up the
approved ones. They share the stack database, so each can run in its own
process. Nothing here deletes a proposal.
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from toolbelt.git.stack.store import StackStore, default_db_path
from toolbelt.proposals.model import (
    Guidelines,
    InvalidProposal,
    Proposal,
    ProposalContent,
    tags_from_data,
)

#: At most this many proposals wait for the user at once: the proposer is not
#: started while there are this many, to bound what proposing costs.
MAX_PENDING = 5


class NoSuchProposal(KeyError):
    """No proposal has this id."""


class AlreadyDecided(Exception):
    """The proposal was decided before this decision arrived."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


class ProposalBook:
    def __init__(
        self,
        *,
        open_store: Callable[[], StackStore] = lambda: StackStore(default_db_path()),
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_pending: int = MAX_PENDING,
    ) -> None:
        self._open = open_store
        self._wall_clock = wall_clock
        self.max_pending = max_pending

    def propose(
        self,
        items: Sequence[tuple[str, str, str, ProposalContent]],
        *,
        run_id: str | None,
        sources_read: Sequence[str] = (),
        model: str = "",
        prompt_version: str = "",
    ) -> list[Proposal]:
        """Stores the items that fit, as ``(source, source_ref, source_url,
        content)``: the cap and earlier proposals of the same source item win
        over what the agent returned. Returns the proposals stored."""
        now = _iso(self._wall_clock())
        proposals = [
            Proposal(
                id=uuid.uuid4().hex,
                source=source,
                source_ref=source_ref,
                source_url=source_url,
                content=content,
                status="pending",
                created_at=now,
                run_id=run_id,
                sources_read=tuple(sources_read),
                model=model,
                prompt_version=prompt_version,
            )
            for source, source_ref, source_url, content in items
        ]
        store = self._open()
        try:
            return store.add_proposals(proposals, self.max_pending)
        finally:
            store.close()

    def all(self) -> list[Proposal]:
        store = self._open()
        try:
            return store.proposals()
        finally:
            store.close()

    def pending(self) -> list[Proposal]:
        store = self._open()
        try:
            return store.proposals(status="pending")
        finally:
            store.close()

    def get(self, proposal_id: str) -> Proposal:
        store = self._open()
        try:
            found = store.proposal(proposal_id)
        finally:
            store.close()
        if found is None:
            raise NoSuchProposal(proposal_id)
        return found

    def approve(self, proposal_id: str) -> Proposal:
        return self._decide(proposal_id, "approved", "", ())

    def reject(
        self, proposal_id: str, *, reason: str = "", tags: object = None
    ) -> Proposal:
        """Rejects with an optional reason and tags (``InvalidProposal`` for
        a tag that is not one of ``REJECT_TAGS``)."""
        return self._decide(
            proposal_id, "rejected", reason.strip(), tags_from_data(tags)
        )

    def record_work(
        self,
        proposal_id: str,
        *,
        task_key: str | None = None,
        branch: str | None = None,
        worker_run_id: str | None = None,
        work_note: str | None = None,
    ) -> None:
        store = self._open()
        try:
            store.set_proposal_work(
                proposal_id,
                task_key=task_key,
                branch=branch,
                worker_run_id=worker_run_id,
                work_note=work_note,
            )
        finally:
            store.close()

    def decided_count(self) -> int:
        return sum(1 for p in self.all() if p.status != "pending")

    def guidelines(self) -> Guidelines | None:
        store = self._open()
        try:
            return store.latest_guidelines()
        finally:
            store.close()

    def write_guidelines(self, body: str, *, author: str) -> None:
        """Adds a new version of the guidelines (an empty body clears them)."""
        count = self.decided_count()
        store = self._open()
        try:
            store.add_guidelines(
                body.strip(), author=author, decisions_seen=count, at=self._wall_clock()
            )
        finally:
            store.close()

    def _decide(
        self, proposal_id: str, status: str, reason: str, tags: tuple[str, ...]
    ) -> Proposal:
        self.get(proposal_id)
        store = self._open()
        try:
            changed = store.decide_proposal(
                proposal_id,
                status=status,
                reason=reason,
                tags=tags,
                at=self._wall_clock(),
            )
        finally:
            store.close()
        if not changed:
            raise AlreadyDecided(proposal_id)
        return self.get(proposal_id)


__all__ = [
    "MAX_PENDING",
    "AlreadyDecided",
    "InvalidProposal",
    "NoSuchProposal",
    "ProposalBook",
]
