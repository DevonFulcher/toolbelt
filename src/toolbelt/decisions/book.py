"""Proposing, answering, reading and deleting decisions.

Everything that touches a decision goes through ``DecisionBook``: the MCP
server an interactive session talks to, the autonomous agents' in-process tool,
the web UI and the dispatcher that resumes a run once its decision is answered.
They share the stack database, so each can run in its own process.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from toolbelt.decisions.model import (
    Decision,
    DecisionPoint,
    InvalidDecision,
    answers_from_data,
)
from toolbelt.git.stack.store import StackStore, default_db_path
from toolbelt.task_claims.resolution import TrackedTask


class NoSuchDecision(KeyError):
    """No decision has this id (it was deleted, or never existed)."""


class AlreadyAnswered(Exception):
    """The decision was answered before this answer arrived."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


class DecisionBook:
    def __init__(
        self,
        *,
        open_store: Callable[[], StackStore] = lambda: StackStore(default_db_path()),
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._open = open_store
        self._wall_clock = wall_clock

    def propose(
        self,
        task: TrackedTask,
        *,
        proposer: str,
        title: str,
        points: tuple[DecisionPoint, ...],
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> Decision:
        if not title.strip():
            raise InvalidDecision("title is required")
        decision = Decision(
            id=uuid.uuid4().hex,
            task_id=task.task_id,
            repo_id=task.repo_id,
            branch=task.branch,
            proposer=proposer,
            session_id=session_id,
            run_id=run_id,
            title=title.strip(),
            points=points,
            status="pending",
            created_at=_iso(self._wall_clock()),
        )
        store = self._open()
        try:
            store.add_decision(decision)
        finally:
            store.close()
        return decision

    def get(self, decision_id: str) -> Decision:
        store = self._open()
        try:
            found = store.decision(decision_id)
        finally:
            store.close()
        if found is None:
            raise NoSuchDecision(decision_id)
        return found

    def for_task(self, task_id: str) -> list[Decision]:
        store = self._open()
        try:
            return store.decisions(task_id=task_id)
        finally:
            store.close()

    def all(self) -> list[Decision]:
        store = self._open()
        try:
            return store.decisions()
        finally:
            store.close()

    def answer(self, decision_id: str, raw_answers: object) -> Decision:
        """Records the user's answers. Raises ``InvalidDecision`` for answers
        that do not fit the points, ``NoSuchDecision``, and ``AlreadyAnswered``."""
        decision = self.get(decision_id)
        answers = answers_from_data(raw_answers, decision.points)
        store = self._open()
        try:
            if not store.answer_decision(decision_id, answers, self._wall_clock()):
                raise AlreadyAnswered(decision_id)
        finally:
            store.close()
        return self.get(decision_id)

    def delete(self, decision_id: str) -> Decision:
        decision = self.get(decision_id)
        store = self._open()
        try:
            store.delete_decision(decision_id)
        finally:
            store.close()
        return decision

    def mark_delivered(self, decision_id: str) -> None:
        store = self._open()
        try:
            store.mark_decision_delivered(decision_id, self._wall_clock())
        finally:
            store.close()
