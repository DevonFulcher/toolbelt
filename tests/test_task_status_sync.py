"""A task's status follows its branch: moved when work starts (append) and when
it lands (merge). Jira is a fake HTTP API: nothing here reaches a real one."""

import json
import logging

import httpx
import pytest

from toolbelt.git import workflow
from toolbelt.git.stack.store import BranchLinks
from toolbelt.tasks.jira import JiraTracker
from toolbelt.tasks.sync_status import sync_task_status
from toolbelt.tasks.tracker import StatusMove

KEY = "RD-100"


class FakeJira:
    """An issue in ``status`` (of Jira status ``category``) with the given
    transitions (name of the status each goes to -> id)."""

    def __init__(
        self,
        status="New",
        category="new",
        transitions=None,
        *,
        issue_code=200,
        assign_code=204,
    ):
        self.assign_code = assign_code
        self.status, self.category = status, category
        self.transitions = transitions or {
            "In Progress": "11",
            "Closed": "21",
            "In Review": "31",
        }
        self.issue_code = issue_code
        self.requests: list[httpx.Request] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "PUT":
            return httpx.Response(self.assign_code)
        if request.url.path.endswith("/myself"):
            return httpx.Response(
                200, json={"accountId": "me-123", "displayName": "Me"}
            )
        if request.method == "POST":
            return httpx.Response(204)
        if request.url.path.endswith("/transitions"):
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": tid, "name": name, "to": {"name": name}}
                        for name, tid in self.transitions.items()
                    ]
                },
            )
        if self.issue_code != 200:
            return httpx.Response(self.issue_code, json={})
        return httpx.Response(
            200,
            json={
                "fields": {
                    "summary": "x",
                    "status": {
                        "name": self.status,
                        "statusCategory": {"key": self.category},
                    },
                }
            },
        )

    @property
    def posted(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    @property
    def assigned(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "PUT"]


def tracker(jira: FakeJira, **kwargs) -> JiraTracker:
    return JiraTracker(
        base_url="https://x.atlassian.net",
        email="me@example.com",
        env={"JIRA_API_TOKEN": "t"},
        client=jira.client,
        **kwargs,
    )


class TestMovingAJiraTask:
    def test_starting_work_moves_a_new_task_to_in_progress(self):
        jira = FakeJira("New", "new")

        result = tracker(jira).move(KEY, "in_progress")

        assert result == StatusMove(
            "moved", f"{KEY}: New -> In Progress, assigned to you"
        )
        assert jira.posted == [{"transition": {"id": "11"}}]

    def test_starting_work_never_moves_a_task_that_is_further_along_back(self):
        jira = FakeJira("In Review", "indeterminate")

        result = tracker(jira).move(KEY, "in_progress")

        assert result.outcome == "skipped" and jira.posted == []

    def test_a_task_already_in_progress_is_left_alone(self):
        jira = FakeJira("In Progress", "indeterminate")

        assert tracker(jira).move(KEY, "in_progress").outcome == "skipped"
        assert jira.posted == []

    def test_landing_moves_the_task_to_the_configured_done_status(self):
        jira = FakeJira("In Review", "indeterminate")

        result = tracker(jira, statuses={"done": "Closed"}).move(KEY, "done")

        assert result == StatusMove(
            "moved", f"{KEY}: In Review -> Closed, assigned to you"
        )
        assert jira.posted == [{"transition": {"id": "21"}}]

    def test_landing_with_the_default_done_status_needs_a_transition_to_it(self):
        jira = FakeJira("In Review", "indeterminate")  # this Jira has no "Done"

        result = tracker(jira).move(KEY, "done")

        assert (
            result.outcome == "failed"
            and "no transition from In Review to Done" in result.message
        )
        assert jira.posted == []

    def test_a_task_that_is_already_done_is_left_alone(self):
        jira = FakeJira("Closed", "done")

        assert (
            tracker(jira, statuses={"done": "Closed"}).move(KEY, "done").outcome
            == "skipped"
        )
        assert jira.posted == []

    def test_a_done_task_in_another_done_status_is_not_reopened_or_changed(self):
        jira = FakeJira("Rejected", "done")

        assert (
            tracker(jira, statuses={"done": "Closed"}).move(KEY, "done").outcome
            == "skipped"
        )

    def test_status_names_match_ignoring_case(self):
        jira = FakeJira("In Review", "indeterminate")

        assert (
            tracker(jira, statuses={"done": "closed"}).move(KEY, "done").outcome
            == "moved"
        )

    def test_an_unknown_status_setting_is_refused(self):
        with pytest.raises(ValueError, match="statuses"):
            JiraTracker(env={}, statuses={"finished": "Closed"})

    def test_an_unreachable_ticket_fails_without_raising(self):
        assert tracker(FakeJira(issue_code=404)).move(KEY, "done").outcome == "failed"
        assert tracker(FakeJira(issue_code=500)).move(KEY, "done").outcome == "failed"

    def test_no_token_fails_without_a_request(self):
        jira = FakeJira()
        t = JiraTracker(base_url="https://x.atlassian.net", env={}, client=jira.client)

        result = t.move(KEY, "in_progress")

        assert result.outcome == "failed" and "JIRA_API_TOKEN" in result.message
        assert jira.requests == []

    def test_a_task_that_is_moved_is_assigned_to_the_account_of_the_token(self):
        jira = FakeJira("New", "new")

        tracker(jira).move(KEY, "in_progress")

        assert jira.assigned == [{"accountId": "me-123"}]
        assert [r.url.path for r in jira.requests if r.method == "PUT"] == [
            f"/rest/api/3/issue/{KEY}/assignee"
        ]

    def test_a_task_that_is_left_alone_is_not_assigned(self):
        jira = FakeJira("In Review", "indeterminate")

        tracker(jira).move(KEY, "in_progress")

        assert jira.assigned == []

    def test_a_task_that_could_not_be_assigned_is_still_moved_and_says_so(self):
        jira = FakeJira("New", "new", assign_code=403)

        result = tracker(jira).move(KEY, "in_progress")

        assert result.outcome == "moved" and "assignee not set" in result.message
        assert jira.posted == [{"transition": {"id": "11"}}]

    def test_a_server_account_is_assigned_by_name(self):
        jira = FakeJira("New", "new")
        original = jira._handle

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/myself"):
                jira.requests.append(request)
                return httpx.Response(200, json={"name": "devon"})
            return original(request)

        jira.client = httpx.Client(transport=httpx.MockTransport(handle))

        tracker(jira).move(KEY, "in_progress")

        assert jira.assigned == [{"name": "devon"}]

    def test_a_ref_that_is_not_an_issue_key_fails(self):
        assert tracker(FakeJira()).move("just words", "done").outcome == "failed"


class Recording:
    name = "jira"

    def __init__(self, outcome="moved"):
        self.calls: list[tuple[str, str]] = []
        self.outcome = outcome

    def move(self, ref, stage):
        self.calls.append((ref, stage))
        return StatusMove(self.outcome, f"{ref}: message")  # type: ignore[arg-type]


class TestInReview:
    def test_a_new_task_whose_pr_is_open_moves_to_in_review(self):
        jira = FakeJira("New", "new")

        result = tracker(jira).move(KEY, "in_review")

        assert result == StatusMove(
            "moved", f"{KEY}: New -> In Review, assigned to you"
        )
        assert jira.posted == [{"transition": {"id": "31"}}]

    def test_a_task_in_progress_moves_to_in_review(self):
        jira = FakeJira("In Progress", "indeterminate")

        assert tracker(jira).move(KEY, "in_review").outcome == "moved"

    def test_a_task_further_along_is_never_moved_back_to_in_review(self):
        for status, category in [
            ("QA", "indeterminate"),
            ("Rollout", "indeterminate"),
            ("Closed", "done"),
        ]:
            jira = FakeJira(status, category)

            assert tracker(jira).move(KEY, "in_review").outcome == "skipped"
            assert jira.posted == []

    def test_the_in_review_status_name_is_configurable(self):
        jira = FakeJira("New", "new", transitions={"Code Review": "41"})

        result = tracker(jira, statuses={"in_review": "Code Review"}).move(
            KEY, "in_review"
        )

        assert result.outcome == "moved" and jira.posted == [
            {"transition": {"id": "41"}}
        ]

    def test_a_task_already_in_review_is_left_alone(self):
        assert (
            tracker(FakeJira("In Review", "indeterminate"))
            .move(KEY, "in_review")
            .outcome
            == "skipped"
        )


class TestReporting:
    def test_nothing_happens_without_a_task_or_a_tracker(self):
        recording = Recording()

        sync_task_status(recording, None, "done")
        sync_task_status(None, KEY, "done")

        assert recording.calls == []

    def test_a_failure_is_a_warning_not_an_error(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.INFO):
            sync_task_status(Recording("failed"), KEY, "done")

        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    def test_a_move_is_reported_as_information(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO):
            sync_task_status(Recording("moved"), KEY, "in_progress")

        assert [r.levelno for r in caplog.records] == [logging.INFO]


class TestMergeFinishesTheTask:
    def test_a_merged_branch_with_a_jira_task_moves_to_done(self, monkeypatch):
        recording = Recording()
        monkeypatch.setattr(
            "toolbelt.tasks.config.configured_tracker", lambda _: recording
        )

        workflow._finish_task(
            BranchLinks(task=KEY, task_tracker="jira", agent_link=None)
        )

        assert recording.calls == [(KEY, "done")]

    def test_a_task_of_another_tracker_or_none_is_left_alone(self, monkeypatch):
        recording = Recording()
        monkeypatch.setattr(
            "toolbelt.tasks.config.configured_tracker", lambda _: recording
        )

        workflow._finish_task(
            BranchLinks(task=KEY, task_tracker="linear", agent_link=None)
        )
        workflow._finish_task(
            BranchLinks(task=None, task_tracker=None, agent_link=None)
        )
        workflow._finish_task(None)

        assert recording.calls == []
