"""The factory UI server: a JSON API over ``FactoryState`` plus ``app.html``."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from toolbelt.software_factory.state import FactoryState, UnknownRecord, WrongStatus

APP_HTML = Path(__file__).with_name("app.html")


def create_app(
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    step_seconds: float = 2.5,
) -> FastAPI:
    state = FactoryState(now)
    app = FastAPI(title="software factory")
    # Held so the replay tasks are not garbage collected mid-run.
    replays: set[asyncio.Task] = set()

    async def replay(run_id: str) -> None:
        while state.step_run(run_id):
            await sleep(step_seconds)

    def checked[T](call: Callable[[], T]) -> T:
        try:
            return call()
        except UnknownRecord as error:
            raise HTTPException(404, f"no such record: {error}") from error
        except WrongStatus as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(APP_HTML)

    @app.get("/api/state")
    def get_state() -> dict:
        return state.snapshot()

    @app.post("/api/work-items/{item_id}/approve", status_code=202)
    async def approve_work_item(item_id: str) -> dict:
        run = checked(lambda: state.approve_work_item(item_id))
        task = asyncio.create_task(replay(run.id))
        replays.add(task)
        task.add_done_callback(replays.discard)
        return {"run_id": run.id}

    @app.post("/api/work-items/{item_id}/reject")
    def reject_work_item(item_id: str) -> dict:
        checked(lambda: state.reject_work_item(item_id))
        return {}

    @app.post("/api/changesets/{changeset_id}/approve")
    def approve_changeset(changeset_id: str) -> dict:
        checked(lambda: state.approve_changeset(changeset_id))
        return {}

    @app.post("/api/changesets/{changeset_id}/reject")
    def reject_changeset(changeset_id: str) -> dict:
        checked(lambda: state.reject_changeset(changeset_id))
        return {}

    @app.post("/api/harness-changes/{change_id}/apply")
    def apply_harness_change(change_id: str) -> dict:
        checked(lambda: state.decide_harness_change(change_id, apply=True))
        return {}

    @app.post("/api/harness-changes/{change_id}/reject")
    def reject_harness_change(change_id: str) -> dict:
        checked(lambda: state.decide_harness_change(change_id, apply=False))
        return {}

    return app
