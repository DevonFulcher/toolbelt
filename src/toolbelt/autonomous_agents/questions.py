"""The questions autonomous runs asked the user, as the dispatcher needs them.

A run that asks ends as ``deciding`` and waits, however long, for the answer in
the stack database: nothing is held open. When the answer is there, the
dispatcher resumes the run's session with it (see ``AgentRunner.resume``). A run
whose question was deleted has nothing to resume with and is settled as stopped.
"""

from collections.abc import Callable

from toolbelt.decisions.model import Decision
from toolbelt.git.stack.store import AgentRun, StackStore


class RunQuestions:
    def __init__(self, open_store: Callable[[], StackStore]) -> None:
        self._open = open_store

    def holding(self) -> set[str]:
        """The tasks with a question of an agent that is not settled: not
        answered yet, or answered and its run not resumed yet. No new run
        starts on them."""
        store = self._open()
        try:
            return {
                d.task_id
                for d in store.decisions()
                if d.status == "pending" or self._awaiting_resume(d)
            }
        finally:
            store.close()

    @staticmethod
    def _awaiting_resume(decision: Decision) -> bool:
        return (
            decision.status == "answered"
            and decision.delivered_at is None
            and decision.proposer == "autonomous"
            and decision.run_id is not None
        )

    def answered(self) -> list[tuple[Decision, AgentRun]]:
        """The answered questions of runs that wait to be resumed with them,
        each with its run."""
        store = self._open()
        try:
            found = []
            for decision in store.decisions():
                if not self._awaiting_resume(decision):
                    continue
                run = store.agent_run(decision.run_id or "")
                if run is not None and run.state == "deciding":
                    found.append((decision, run))
            return found
        finally:
            store.close()

    def settle_orphans(self) -> list[str]:
        """Settles the ``deciding`` runs whose question was deleted; returns
        their ids."""
        store = self._open()
        try:
            asked = {d.run_id for d in store.decisions()}
            orphans = [r.run_id for r in store.deciding_runs() if r.run_id not in asked]
            for run_id in orphans:
                store.settle_deciding_run(
                    run_id, state="stopped", error="its question was deleted"
                )
            return orphans
        finally:
            store.close()
