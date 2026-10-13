"""Keeps the spend ledger current while ``tt ui`` runs, and answers the spend
view from it.

Like the claims feed it does not wait for a browser: reading transcripts is
local, and the ledger is only as complete as the reads. Each pass reads what
was appended to the session transcripts since the last one (see
``token_spend.ingest``); the first pass over a long history takes a few
seconds, so it runs off the event loop.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from toolbelt.git.stack.store import StackStore, TokenCounts
from toolbelt.token_spend.ingest import ingest_transcripts
from toolbelt.token_spend.summary import SpendSummary, summarize
from toolbelt.ui import api_models as api

DEFAULT_INTERVAL_SECONDS = 60.0
#: The breakdown windows the endpoint accepts, in days.
BREAKDOWN_DAYS = (1, 7, 30)
DEFAULT_BREAKDOWN_DAYS = 7


def _tokens(tokens: TokenCounts) -> api.SpendTokens:
    return api.SpendTokens(
        input=tokens.input,
        output=tokens.output,
        cache_read=tokens.cache_read,
        cache_write_5m=tokens.cache_write_5m,
        cache_write_1h=tokens.cache_write_1h,
    )


def to_api(summary: SpendSummary, ingested_at: datetime | None) -> api.Spend:
    return api.Spend(
        as_of=summary.as_of.isoformat(),
        ingested_at=ingested_at.isoformat() if ingested_at else None,
        breakdown_days=summary.breakdown_days,
        windows=[
            api.SpendWindow(
                label=w.label,
                start=w.start.isoformat(),
                calls=w.calls,
                tokens=_tokens(w.tokens),
                cost_usd=w.cost_usd,
                unpriced_calls=w.unpriced_calls,
            )
            for w in summary.windows
        ],
        by_model=[
            api.SpendModel(
                model=m.model,
                calls=m.calls,
                tokens=_tokens(m.tokens),
                cost_usd=m.cost_usd,
                unpriced_calls=m.unpriced_calls,
            )
            for m in summary.by_model
        ],
        by_task=[
            api.SpendTask(
                task_id=t.task_id,
                repo=t.repo,
                branch=t.branch,
                calls=t.calls,
                tokens=t.tokens,
                cost_usd=t.cost_usd,
                unpriced_calls=t.unpriced_calls,
            )
            for t in summary.by_task
        ],
        unpriced_models=[
            api.UnpricedModel(model=model, calls=calls)
            for model, calls in summary.unpriced_models.items()
        ],
        claims_since=summary.claims_since.isoformat() if summary.claims_since else None,
        autonomous=api.AutonomousSpend(
            windows=[
                api.AutonomousWindow(
                    label=w.label,
                    start=w.start.isoformat(),
                    runs=w.runs,
                    cost_usd=w.cost_usd,
                    tokens=_tokens(w.tokens),
                )
                for w in summary.autonomous.windows
            ],
            by_trigger=[
                api.AutonomousTrigger(
                    trigger=t.trigger,  # type: ignore[arg-type]
                    runs=t.runs,
                    cost_usd=t.cost_usd,
                )
                for t in summary.autonomous.by_trigger
            ],
            by_task=[
                api.AutonomousTask(
                    task_id=t.task_id,
                    repo=t.repo,
                    branch=t.branch,
                    runs=t.runs,
                    cost_usd=t.cost_usd,
                )
                for t in summary.autonomous.by_task
            ],
        ),
    )


class SpendFeed:
    def __init__(
        self,
        *,
        db_path: Callable[[], Path],
        projects: Callable[[], Path],
        wall_clock: Callable[[], datetime],
        interval: float = DEFAULT_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._db_path = db_path
        self._projects = projects
        self._wall_clock = wall_clock
        self._interval = interval
        self._sleep = sleep
        #: When the last pass over the transcripts ended (None: none yet).
        self.ingested_at: datetime | None = None

    def ingest(self) -> None:
        """One pass over the transcripts (blocking)."""
        store = StackStore(self._db_path())
        try:
            ingest_transcripts(store, self._projects())
        finally:
            store.close()
        self.ingested_at = self._wall_clock()

    def spend(self, days: int) -> api.Spend:
        """The spend view's data, from what has been ingested (blocking)."""
        store = StackStore(self._db_path())
        try:
            summary = summarize(store, self._wall_clock(), breakdown_days=days)
        finally:
            store.close()
        return to_api(summary, self.ingested_at)

    async def run(self) -> None:
        while True:
            await asyncio.to_thread(self.ingest)
            await self._sleep(self._interval)
