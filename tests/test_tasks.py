"""The task tracker interface, its Jira implementation, and config selection.

HTTP goes through an `httpx.MockTransport` and the token command through an
injected runner, so nothing here touches Jira or spawns a process.
"""

import base64
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from toolbelt.tasks.config import configured_tracker
from toolbelt.tasks.jira import JiraTracker
from toolbelt.tasks.tracker import TaskInfo

_ISSUE = {"fields": {"summary": "Fix login", "status": {"name": "In Progress"}}}


class FakeJira:
    """A Jira API: records requests, answers with ``response``."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.response = response or httpx.Response(200, json=_ISSUE)
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


class FakeCommands:
    """A token-command runner returning ``output`` (or raising ``error``)."""

    def __init__(self, output: str = "", error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(list(argv))
        if self.error:
            raise self.error
        return self.output


def tracker_with(jira: FakeJira, **kwargs) -> JiraTracker:
    kwargs.setdefault("base_url", "https://x.atlassian.net")
    kwargs.setdefault("email", "me@example.com")
    kwargs.setdefault("env", {"JIRA_API_TOKEN": "env-token"})
    return JiraTracker(client=jira.client, **kwargs)


class TestUrls:
    def test_key_becomes_browse_url(self):
        t = JiraTracker(base_url="https://j.example/", env={})
        assert t.url_for("ABC-1") == "https://j.example/browse/ABC-1"

    def test_key_without_base_url_has_no_link(self):
        assert JiraTracker(env={}).url_for("ABC-1") is None

    def test_url_is_used_as_is(self):
        t = JiraTracker(base_url="https://other", env={})
        url = "https://j.example/browse/ABC-1?x=1"
        assert t.url_for(url) == url
        assert JiraTracker(env={}).url_for(url) == url

    def test_non_http_schemes_are_not_linked(self):
        assert JiraTracker(base_url="https://j", env={}).url_for("javascript:x") is None

    def test_display_ref_extracts_key_from_url(self):
        t = JiraTracker(env={})
        assert t.display_ref("https://j.example/browse/ABC-1") == "ABC-1"
        assert t.display_ref("ABC-1") == "ABC-1"
        assert t.display_ref("https://j.example/other") == "https://j.example/other"


class TestFetch:
    def test_cloud_basic_auth_with_env_token(self):
        jira = FakeJira()
        t = tracker_with(jira)

        info = t.fetch("ABC-1")

        assert info == TaskInfo(
            ref="ABC-1",
            title="Fix login",
            status="In Progress",
            url="https://x.atlassian.net/browse/ABC-1",
        )
        (request,) = jira.requests
        assert request.url.path == "/rest/api/3/issue/ABC-1"
        assert request.url.params["fields"] == "summary,status"
        expected = base64.b64encode(b"me@example.com:env-token").decode()
        assert request.headers["Authorization"] == f"Basic {expected}"

    def test_server_bearer_auth_and_api_v2(self):
        jira = FakeJira()
        t = tracker_with(jira, auth="bearer", api_version=2, email=None)

        assert t.fetch("ABC-1") is not None

        (request,) = jira.requests
        assert request.url.path == "/rest/api/2/issue/ABC-1"
        assert request.headers["Authorization"] == "Bearer env-token"

    def test_url_ref_fetches_by_its_key(self):
        jira = FakeJira()
        info = tracker_with(jira).fetch("https://other.example/browse/ABC-7")
        assert info is not None and info.ref == "ABC-7"
        assert jira.requests[0].url.path.endswith("/issue/ABC-7")

    def test_token_command_wins_over_env_and_runs_once(self):
        jira = FakeJira()
        commands = FakeCommands(output="cmd-token\n")
        t = tracker_with(
            jira,
            token_command=["op", "read", "op://Private/Jira/token"],
            run_command=commands,
        )

        t.fetch("ABC-1")
        t.fetch("ABC-2")

        assert commands.calls == [["op", "read", "op://Private/Jira/token"]]
        expected = base64.b64encode(b"me@example.com:cmd-token").decode()
        assert all(
            r.headers["Authorization"] == f"Basic {expected}" for r in jira.requests
        )

    def test_failed_token_command_falls_back_to_env(self):
        jira = FakeJira()
        commands = FakeCommands(error=subprocess.CalledProcessError(1, ["op"]))
        t = tracker_with(jira, token_command=["op"], run_command=commands)

        assert t.fetch("ABC-1") is not None

        expected = base64.b64encode(b"me@example.com:env-token").decode()
        assert jira.requests[0].headers["Authorization"] == f"Basic {expected}"

    def test_missing_command_binary_falls_back_to_env(self):
        jira = FakeJira()
        commands = FakeCommands(error=FileNotFoundError("op"))
        t = tracker_with(jira, token_command=["op"], run_command=commands)
        assert t.fetch("ABC-1") is not None

    def test_no_token_means_no_request(self):
        jira = FakeJira()
        t = tracker_with(jira, env={})
        assert t.fetch("ABC-1") is None
        assert jira.requests == []

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(401),
            httpx.Response(404, json={"errorMessages": ["nope"]}),
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"unexpected": True}),
        ],
    )
    def test_failures_return_none(self, response):
        assert tracker_with(FakeJira(response)).fetch("ABC-1") is None

    def test_transport_errors_return_none(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        client = httpx.Client(transport=httpx.MockTransport(boom))
        t = JiraTracker(
            base_url="https://x", email="e", env={"JIRA_API_TOKEN": "t"}, client=client
        )
        assert t.fetch("ABC-1") is None

    def test_unfetchable_refs_make_no_request(self):
        jira = FakeJira()
        t = tracker_with(jira)
        assert t.fetch("not a key") is None
        assert tracker_with(jira, base_url=None).fetch("ABC-1") is None
        assert jira.requests == []

    def test_unknown_auth_mode_is_rejected(self):
        with pytest.raises(ValueError, match="auth"):
            JiraTracker(auth="oauth", env={})


class TestConfig:
    def write(self, tmp_path: Path, text: str) -> Path:
        config = tmp_path / "config.yaml"
        config.write_text(text)
        return config

    def test_no_file_or_no_tasks_section_means_no_tracker(self, tmp_path):
        assert configured_tracker(tmp_path / "missing.yaml") is None
        assert configured_tracker(self.write(tmp_path, "sync:\n  repos: []\n")) is None
        assert configured_tracker(self.write(tmp_path, "tasks: {}\n")) is None

    def test_jira_section_is_parsed(self, tmp_path):
        jira = FakeJira()
        commands = FakeCommands(output="tok")
        config = self.write(
            tmp_path,
            "tasks:\n"
            "  tracker: jira\n"
            "  jira:\n"
            "    base_url: https://jira.corp.example\n"
            "    auth: bearer\n"
            "    api_version: 2\n"
            "    token_command: [op, read, op://Private/Jira/token]\n",
        )

        tracker = configured_tracker(
            config, env={}, client=jira.client, run_command=commands
        )

        assert tracker is not None and tracker.name == "jira"
        assert tracker.url_for("ABC-1") == "https://jira.corp.example/browse/ABC-1"
        assert tracker.fetch("ABC-1") is not None
        request = jira.requests[0]
        assert request.url.path == "/rest/api/2/issue/ABC-1"
        assert request.headers["Authorization"] == "Bearer tok"
        assert commands.calls == [["op", "read", "op://Private/Jira/token"]]

    def test_unknown_tracker_is_an_error(self, tmp_path):
        config = self.write(tmp_path, "tasks:\n  tracker: trello\n")
        with pytest.raises(ValueError, match="trello"):
            configured_tracker(config)

    def test_token_command_must_be_an_argv_list(self, tmp_path):
        config = self.write(
            tmp_path,
            "tasks:\n  tracker: jira\n  jira:\n    token_command: op read x\n",
        )
        with pytest.raises(ValueError, match="token_command"):
            configured_tracker(config)

    def test_the_token_is_not_a_config_key(self, tmp_path):
        """Only the *command* is configurable; a literal token is ignored."""
        jira = FakeJira()
        config = self.write(
            tmp_path,
            "tasks:\n  tracker: jira\n  jira:\n"
            "    base_url: https://x\n    email: e\n    token: literal-secret\n",
        )
        tracker = configured_tracker(config, env={}, client=jira.client)
        assert tracker is not None
        assert tracker.fetch("ABC-1") is None
        assert jira.requests == []


def test_default_token_command_runner_is_shell_free():
    """The real runner execs the argv directly (no shell expansion)."""
    from toolbelt.tasks.jira import run_token_command

    out = run_token_command(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME && x"]
    )

    assert out.strip() == "$HOME && x"
