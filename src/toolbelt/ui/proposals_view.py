"""The Proposals view's data, as the API's wire models."""

from collections.abc import Callable
from dataclasses import asdict

from toolbelt.proposals.book import ProposalBook
from toolbelt.proposals.model import REJECT_TAGS, Proposal
from toolbelt.ui import api_models as api


def work_text(proposal: Proposal, run_state: str | None = None) -> str | None:
    """What the worker is doing with an approved proposal, in a few words
    (``run_state``: the state of its run, when it has one)."""
    if proposal.status != "approved":
        return None
    if proposal.work_note:
        return f"The worker could not take it up: {proposal.work_note}"
    if proposal.worker_run_id is None:
        return "Waiting for a worker"
    if run_state == "running":
        return f"A worker is implementing it on {proposal.branch}"
    if run_state == "deciding":
        return f"The worker asked you a question about {proposal.branch}"
    if run_state == "succeeded":
        return f"Branch {proposal.branch} is ready for your review"
    return f"The worker's run ended ({run_state}) on {proposal.branch}"


def proposal_info(proposal: Proposal, run_state: str | None = None) -> api.ProposalInfo:
    content = asdict(proposal.content)
    return api.ProposalInfo(
        id=proposal.id,
        source=proposal.source,
        source_ref=proposal.source_ref,
        source_url=proposal.source_url,
        content=api.ProposalContentInfo(**content),
        status=proposal.status,
        created_at=proposal.created_at,
        decided_at=proposal.decided_at,
        decision_reason=proposal.decision_reason,
        decision_tags=list(proposal.decision_tags),  # type: ignore[arg-type]
        task_key=proposal.task_key,
        branch=proposal.branch,
        work=work_text(proposal, run_state),
    )


def build_proposals(
    book: ProposalBook,
    proposer_status: Callable[[], str],
    run_states: Callable[[], dict[str, str]] = dict,
) -> api.ProposalList:
    """``run_states``: the state of each worker run, by run id."""
    states = run_states()
    return api.ProposalList(
        proposals=[
            proposal_info(p, states.get(p.worker_run_id or "")) for p in book.all()
        ],
        max_pending=book.max_pending,
        proposer=proposer_status(),
        tags=list(REJECT_TAGS),  # type: ignore[arg-type]
        guidelines=(
            api.ProposalGuidelines(
                body=found.body,
                author=found.author,  # type: ignore[arg-type]
                decisions_seen=found.decisions_seen,
                created_at=found.created_at,
            )
            if (found := book.guidelines())
            else None
        ),
    )
