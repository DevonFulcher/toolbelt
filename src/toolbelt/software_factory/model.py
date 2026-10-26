"""The records the factory UI shows.

Plain dataclasses that serialize with ``dataclasses.asdict``. Times are ISO
strings so the UI can render them without conversion.
"""

from dataclasses import dataclass, field
from typing import Literal


def diff_lines(rows: list[str]) -> str:
    return "\n".join(rows)


WorkItemStatus = Literal[
    "proposed", "implementing", "changeset_ready", "pr_opened", "rejected"
]
ChangesetStatus = Literal["ready", "pr_opened", "rejected"]
HarnessChangeStatus = Literal["proposed", "applied", "rejected"]
RunStatus = Literal["running", "done"]


@dataclass
class McpSource:
    """An MCP system the factory reads on a schedule."""

    id: str
    name: str
    server: str
    schedule: str
    last_read_at: str
    next_read_at: str
    items_read: int
    proposed: int
    healthy: bool = True


@dataclass
class WorkItem:
    id: str
    title: str
    summary: str
    #: ``McpSource.id`` the item was proposed from.
    source_id: str
    #: What in the source prompted it (a ticket key, an alert id, a thread).
    source_ref: str
    source_url: str
    rationale: str
    repo: str
    size: Literal["S", "M", "L"]
    proposed_at: str
    status: WorkItemStatus = "proposed"
    run_id: str | None = None
    changeset_id: str | None = None
    pr_url: str | None = None


@dataclass
class FileChange:
    path: str
    additions: int
    deletions: int
    #: Unified diff hunk(s); lines start with ``+``, ``-``, ``@@`` or a space.
    diff: str


@dataclass
class Changeset:
    id: str
    work_item_id: str
    branch: str
    summary: str
    files: list[FileChange]
    tests: str
    status: ChangesetStatus = "ready"


@dataclass
class RunEvent:
    at: str
    #: ``read``, ``skill``, ``tool``, ``edit``, ``test``, ``message``.
    kind: str
    text: str
    tokens: int = 0


@dataclass
class AgentRun:
    id: str
    work_item_id: str
    model: str
    started_at: str
    status: RunStatus = "running"
    events: list[RunEvent] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(event.tokens for event in self.events)


@dataclass
class HarnessChange:
    """A proposed edit to the agent harness itself (a skill, a hook, a prompt)."""

    id: str
    title: str
    target: str
    rationale: str
    #: Run ids that motivated it.
    evidence: list[str]
    diff: str
    proposed_at: str
    status: HarnessChangeStatus = "proposed"
