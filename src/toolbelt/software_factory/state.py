"""The factory's in-memory state and its lifecycle.

A work item is proposed from an MCP source. Approving it starts an agent run;
when the run finishes it leaves a changeset; approving the changeset opens a
PR. Today the "agent" replays a canned script (``step_run``), so the UI can be
seen working before the real runner, MCP readers and memory store exist.
"""

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime

from toolbelt.software_factory import fake_data
from toolbelt.software_factory.model import (
    AgentRun,
    Changeset,
    FileChange,
    HarnessChange,
    McpSource,
    RunEvent,
    WorkItem,
    diff_lines,
)

#: What the replayed agent does, in order: (kind, text, tokens).
SCRIPT: list[tuple[str, str, int]] = [
    ("read", "Read {source_ref} from {source_id}", 1800),
    ("skill", "Loaded skill `python`", 900),
    ("tool", "grep -rn for the code the item mentions", 500),
    ("message", "Planning a minimal change that addresses: {title}", 2100),
    ("edit", "Edit the implementation (+14 −4)", 3600),
    ("edit", "Add a test that fails without the change (+19)", 2400),
    ("test", "pytest: all passed", 800),
]


class UnknownRecord(KeyError):
    pass


class WrongStatus(ValueError):
    pass


class FactoryState:
    def __init__(self, now: Callable[[], datetime]):
        self._now = now
        at = now()
        self.sources: list[McpSource] = fake_data.sources(at)
        self.work_items: list[WorkItem] = fake_data.work_items(at)
        self.runs: list[AgentRun] = fake_data.runs(at)
        self.changesets: list[Changeset] = fake_data.changesets()
        self.harness_changes: list[HarnessChange] = fake_data.harness_changes(at)

    def snapshot(self) -> dict:
        return {
            "now": self._now().isoformat(timespec="seconds"),
            "sources": [asdict(s) for s in self.sources],
            "work_items": [asdict(w) for w in self.work_items],
            "runs": [{**asdict(r), "tokens": r.tokens} for r in self.runs],
            "changesets": [asdict(c) for c in self.changesets],
            "harness_changes": [asdict(h) for h in self.harness_changes],
        }

    def approve_work_item(self, item_id: str) -> AgentRun:
        item = self._work_item(item_id)
        self._require(item.status, "proposed")
        run = AgentRun(
            f"run-{len(self.runs) + 1}",
            item.id,
            "claude-sonnet-5-5",
            self._stamp(),
        )
        self.runs.append(run)
        item.status = "implementing"
        item.run_id = run.id
        return run

    def reject_work_item(self, item_id: str) -> None:
        item = self._work_item(item_id)
        self._require(item.status, "proposed")
        item.status = "rejected"

    def step_run(self, run_id: str) -> bool:
        """Replay the agent's next step; returns whether more steps remain."""
        run = self._run(run_id)
        item = self._work_item(run.work_item_id)
        kind, text, tokens = SCRIPT[len(run.events)]
        run.events.append(
            RunEvent(
                self._stamp(),
                kind,
                text.format(
                    source_ref=item.source_ref,
                    source_id=item.source_id,
                    title=item.title,
                ),
                tokens,
            )
        )
        if len(run.events) < len(SCRIPT):
            return True
        run.status = "done"
        changeset = self._propose_changeset(item)
        item.status = "changeset_ready"
        item.changeset_id = changeset.id
        return False

    def approve_changeset(self, changeset_id: str) -> None:
        changeset = self._changeset(changeset_id)
        self._require(changeset.status, "ready")
        item = self._work_item(changeset.work_item_id)
        changeset.status = "pr_opened"
        item.status = "pr_opened"
        item.pr_url = (
            f"https://github.com/example/{item.repo}/pull/{420 + len(self.changesets)}"
        )

    def reject_changeset(self, changeset_id: str) -> None:
        changeset = self._changeset(changeset_id)
        self._require(changeset.status, "ready")
        changeset.status = "rejected"
        self._work_item(changeset.work_item_id).status = "rejected"

    def decide_harness_change(self, change_id: str, *, apply: bool) -> None:
        change = next((h for h in self.harness_changes if h.id == change_id), None)
        if change is None:
            raise UnknownRecord(change_id)
        self._require(change.status, "proposed")
        change.status = "applied" if apply else "rejected"

    def _propose_changeset(self, item: WorkItem) -> Changeset:
        changeset = Changeset(
            f"cs-{len(self.changesets) + 1}",
            item.id,
            f"devon/{item.id}",
            f"Implements: {item.title}.",
            [
                FileChange(
                    "src/toolbelt/example.py",
                    14,
                    4,
                    diff_lines(
                        [
                            "@@ -10,4 +10,14 @@",
                            "-    old_behavior()",
                            "+    new_behavior()",
                            "+    # sample diff: the real agent's edit goes here",
                        ]
                    ),
                )
            ],
            "pytest: all passed",
        )
        self.changesets.append(changeset)
        return changeset

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    @staticmethod
    def _require(actual: str, expected: str) -> None:
        if actual != expected:
            raise WrongStatus(f"is {actual}, not {expected}")

    def _work_item(self, item_id: str) -> WorkItem:
        return self._find(self.work_items, item_id)

    def _run(self, run_id: str) -> AgentRun:
        return self._find(self.runs, run_id)

    def _changeset(self, changeset_id: str) -> Changeset:
        return self._find(self.changesets, changeset_id)

    @staticmethod
    def _find[T: (WorkItem, AgentRun, Changeset)](
        records: list[T], record_id: str
    ) -> T:
        for record in records:
            if record.id == record_id:
                return record
        raise UnknownRecord(record_id)
