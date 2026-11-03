import asyncio
from datetime import UTC, datetime

from toolbelt.tasks.tracker import TaskInfo, TaskStatusChange, TaskStatusHistory
from toolbelt.ui.live_status import EventHub
from toolbelt.ui.task_cache import TaskInfoCache
from toolbelt.ui.task_feed import LinkedTask, TaskFeed


class Jira:
    name = "jira"

    def __init__(self) -> None:
        self.history_calls = 0
        self.history = TaskStatusHistory(
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            current_status="In Review",
            fetched_at=datetime(2026, 1, 2, tzinfo=UTC),
            changes=(
                TaskStatusChange(
                    "1", datetime(2026, 1, 2, tzinfo=UTC), "To Do", "In Review"
                ),
            ),
            truncated_before=False,
        )

    def fetch(self, ref: str) -> TaskInfo:
        return TaskInfo(ref, "Fix a thing", "In Review", None)

    def status_history(self, ref: str) -> TaskStatusHistory:
        self.history_calls += 1
        return self.history

    def unavailable_reason(self) -> None:
        return None

    def url_for(self, ref: str) -> str:
        return ref

    def display_ref(self, ref: str) -> str:
        return ref

    def move(self, ref: str, stage: str):
        raise AssertionError("TaskFeed should not change Jira status")


def test_status_history_is_recorded_in_the_background_cache_interval():
    jira = Jira()
    task = LinkedTask(1, "branch", "RD-100", jira)
    now = [0.0]
    recorded: list[TaskStatusHistory] = []
    notifications: list[bool] = []

    def record(linked: LinkedTask, history: TaskStatusHistory) -> bool:
        recorded.append(history)
        return True

    feed = TaskFeed(
        tasks=lambda: [task],
        cache=TaskInfoCache(clock=lambda: now[0]),
        hub=EventHub(clock=lambda: now[0]),
        record_history=record,
        on_history_change=lambda: notifications.append(True),
        history_ttl=10.0,
        clock=lambda: now[0],
    )

    asyncio.run(feed.run_once())
    asyncio.run(feed.run_once())
    assert jira.history_calls == 1
    assert recorded == [jira.history]
    assert notifications == [True]

    now[0] = 11.0
    asyncio.run(feed.run_once())
    assert jira.history_calls == 2
    assert recorded == [jira.history, jira.history]
    assert notifications == [True, True]
