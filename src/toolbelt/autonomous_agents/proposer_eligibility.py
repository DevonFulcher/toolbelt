"""When the proposer agent may look for proposals, and when it waits and why.

A pure function over what the feed has gathered, so every wait is testable and
the Proposals view can show the same sentence the feed acted on. The agents'
shared blockers (switches, spend limits, sign-in, the screen) are the
dispatcher's and are applied by the feed before this.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from toolbelt.git.stack.store import AgentRun
from toolbelt.proposals.settings import ProposalSettings


@dataclass(frozen=True)
class ProposerWait:
    text: str
    #: When the wait ends by itself (``None``: when something else changes).
    until: datetime | None = None


def proposer_wait(
    *,
    now: datetime,
    settings: ProposalSettings,
    pending: int,
    running: bool,
    last_run: AgentRun | None,
    last_stored: int,
    last_decided_at: datetime | None,
) -> ProposerWait | None:
    """Why the proposer does not start now (``None``: it starts).

    It looks when a slot is free and either the cadence has passed since the
    last look, or the user has decided a proposal since then (after a short
    settle delay, so several decisions are answered by one look) and the last
    look filled every slot it was asked for. A look that came back short found
    nothing more worth proposing, and one that failed should not be repeated
    on every decision, so neither is repeated before the cadence.
    ``last_stored`` is how many proposals the last look stored.
    """
    if pending >= settings.max_pending:
        return ProposerWait(
            f"{pending} proposals are waiting for you: not looking for more."
        )
    if running:
        return ProposerWait("Looking for proposals…")
    if last_run is None:
        return None
    cadence_at = last_run.started_at + timedelta(hours=settings.cadence_hours)
    if now >= cadence_at:
        return None
    if last_run.state != "succeeded":
        return ProposerWait("The last look failed. Trying again later.", cadence_at)
    if last_run.slots is not None and last_stored < last_run.slots:
        return ProposerWait(
            "The last look found nothing more worth proposing. Looking again later.",
            cadence_at,
        )
    if last_decided_at is not None and last_decided_at > last_run.started_at:
        settled_at = last_decided_at + timedelta(seconds=settings.settle_seconds)
        if now >= settled_at:
            return None
        return ProposerWait(
            "Looking again shortly: you decided a proposal.", settled_at
        )
    return ProposerWait("Looking again later.", cadence_at)
