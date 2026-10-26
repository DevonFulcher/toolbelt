"""What a proposer agent proposes and what the user decides about it.

A proposal is never deleted: the user's decision (approve or reject, with an
optional reason and tags) is recorded on the row, and decided proposals are only
hidden by the UI. The proposals, with their decisions, are the record the
proposer learns from.

The shapes are plain dataclasses so that reading them back from the database
stays cheap at start-up; ``content_from_data`` validates what an agent sent.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

type ProposalStatus = Literal["pending", "approved", "rejected"]

SIZES = ("S", "M", "L")
CONFIDENCES = ("low", "medium", "high")
#: Quick reasons for a rejection; the free-text reason carries the rest.
REJECT_TAGS = (
    "not_relevant",
    "too_big",
    "already_done",
    "wrong_repo",
    "low_value",
    "unclear",
)


@dataclass(frozen=True)
class Guidelines:
    """What the proposer is told the user wants, learned from the decisions: a
    new version is added each time (a curator run writes one, or the user edits
    it); older versions are kept."""

    id: int
    body: str
    #: ``curator`` or ``user``.
    author: str
    #: How many decided proposals existed when it was written.
    decisions_seen: int
    created_at: str


class InvalidProposal(ValueError):
    """What was proposed or decided does not fit the shape."""


@dataclass(frozen=True)
class Evidence:
    quote: str
    url: str = ""
    author: str = ""
    date: str = ""


@dataclass(frozen=True)
class ProposalContent:
    """What the proposer wrote: enough to decide on without opening the source."""

    title: str
    #: What and why, a short paragraph.
    summary: str
    problem: str
    #: Why this is worth doing now rather than later.
    why_now: str
    evidence: tuple[Evidence, ...]
    #: How the change would be made: the areas of code and the steps.
    approach: str
    acceptance_criteria: tuple[str, ...]
    risks: tuple[str, ...]
    size: str
    confidence: str
    #: The tracked repo the work is in.
    repo: str
    #: Existing PRs, branches or tickets that touch the same thing.
    related: tuple[str, ...]
    #: What the worker will do if the user approves.
    on_approve: str


@dataclass(frozen=True)
class Proposal:
    id: str
    #: The MCP system it came from (``jira``, ``github``, ...) and what in it
    #: prompted it (a ticket key, an alert id, a thread). Together they are
    #: unique, so a source item is proposed once, whatever the user decides.
    source: str
    source_ref: str
    source_url: str
    content: ProposalContent
    status: ProposalStatus
    created_at: str
    #: The proposer run that wrote it.
    run_id: str | None = None
    decided_at: str | None = None
    decision_reason: str = ""
    decision_tags: tuple[str, ...] = ()
    #: The Jira ticket the worker's branch is for, once the worker has one.
    task_key: str | None = None
    #: The worker's branch, once it has made one.
    branch: str | None = None
    #: The worker's run, once one has started.
    worker_run_id: str | None = None
    #: Why the worker could not take the proposal up (empty: nothing wrong).
    work_note: str = ""
    #: The systems the proposer read to produce it, and the model and prompt
    #: version that did: what a later look at the decisions needs to know.
    sources_read: tuple[str, ...] = ()
    model: str = ""
    prompt_version: str = ""


def _text(data: dict[str, Any], key: str, *, required: bool = True) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise InvalidProposal(f"{key} must be text")
    if required and not value.strip():
        raise InvalidProposal(f"{key} is required")
    return value.strip()


def _texts(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise InvalidProposal(f"{key} must be a list of text")
    return tuple(v.strip() for v in value if v.strip())


def content_from_data(data: object) -> ProposalContent:
    """Validates a proposal's content as an agent wrote it (or as it was stored)."""
    if not isinstance(data, dict):
        raise InvalidProposal("a proposal must be an object")
    raw_evidence = data.get("evidence", [])
    if not isinstance(raw_evidence, list):
        raise InvalidProposal("evidence must be a list")
    evidence = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            raise InvalidProposal("each piece of evidence must be an object")
        evidence.append(
            Evidence(
                quote=_text(item, "quote"),
                url=_text(item, "url", required=False),
                author=_text(item, "author", required=False),
                date=_text(item, "date", required=False),
            )
        )
    size = _text(data, "size")
    if size not in SIZES:
        raise InvalidProposal(f"size must be one of {', '.join(SIZES)}")
    confidence = _text(data, "confidence")
    if confidence not in CONFIDENCES:
        raise InvalidProposal(f"confidence must be one of {', '.join(CONFIDENCES)}")
    return ProposalContent(
        title=_text(data, "title"),
        summary=_text(data, "summary"),
        problem=_text(data, "problem"),
        why_now=_text(data, "why_now"),
        evidence=tuple(evidence),
        approach=_text(data, "approach"),
        acceptance_criteria=_texts(data, "acceptance_criteria"),
        risks=_texts(data, "risks"),
        size=size,
        confidence=confidence,
        repo=_text(data, "repo"),
        related=_texts(data, "related"),
        on_approve=_text(data, "on_approve"),
    )


def content_to_json(content: ProposalContent) -> str:
    return json.dumps(asdict(content))


def content_from_json(text: str) -> ProposalContent:
    return content_from_data(json.loads(text))


def tags_from_data(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise InvalidProposal("tags must be a list of text")
    unknown = [v for v in value if v not in REJECT_TAGS]
    if unknown:
        raise InvalidProposal(f"unknown tags: {', '.join(unknown)}")
    return tuple(dict.fromkeys(value))
