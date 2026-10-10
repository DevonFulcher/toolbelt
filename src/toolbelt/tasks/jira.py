"""The Jira implementation of ``TaskTracker``.

Configured under ``tasks.jira`` in toolbelt's config file (see
``toolbelt.tasks.config``). The API token is never stored there: it is read
from the output of ``token_command`` (an argv list run without a shell, e.g.
``[op, read, op://Private/Jira/token]``), falling back to the
``JIRA_API_TOKEN`` environment variable.

* Jira Cloud: ``auth: basic`` (the default) — ``email`` + API token, REST
  API v3.
* Jira Server / Data Center: ``auth: bearer`` — a personal access token, with
  ``api_version: 2``.

A stored URL is linked as-is; a stored key becomes ``<base_url>/browse/<key>``
(plain text when ``base_url`` is unset).
"""

import re
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from toolbelt import logged_process
from toolbelt.tasks.tracker import TaskInfo

TOKEN_ENV_VAR = "JIRA_API_TOKEN"
_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*-\d+")
_REQUEST_TIMEOUT_SECONDS = 10
_AUTH_MODES = ("basic", "bearer")

# Runs an argv and returns its stdout.
CommandRunner = Callable[[Sequence[str]], str]


def run_token_command(argv: Sequence[str]) -> str:
    return logged_process.run(
        list(argv), capture_output=True, text=True, check=True
    ).stdout


class JiraTracker:
    name = "jira"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        email: str | None = None,
        auth: str = "basic",
        api_version: int = 3,
        token_command: Sequence[str] | None = None,
        env: Mapping[str, str],
        run_command: CommandRunner = run_token_command,
        client: httpx.Client | None = None,
    ) -> None:
        if auth not in _AUTH_MODES:
            raise ValueError(
                f"tasks.jira.auth must be one of {_AUTH_MODES}, not {auth!r}"
            )
        self._base_url = base_url.rstrip("/") if base_url else None
        self._email = email
        self._auth = auth
        self._api_version = api_version
        self._token_command = list(token_command) if token_command else None
        self._env = env
        self._run_command = run_command
        self._client = client
        # The token command may prompt (a password manager), so it runs at
        # most once per tracker, even when it yields nothing.
        self._token_lock = threading.Lock()
        self._token_resolved = False
        self._token: str | None = None

    @classmethod
    def from_config(cls, section: Mapping[str, Any], **kwargs: Any) -> "JiraTracker":
        """``section`` is the ``tasks.jira`` mapping from the config file."""
        return cls(
            base_url=_optional_str(section.get("base_url")),
            email=_optional_str(section.get("email")),
            auth=str(section.get("auth") or "basic"),
            api_version=int(section.get("api_version") or 3),
            token_command=_argv(section.get("token_command")),
            **kwargs,
        )

    def url_for(self, ref: str) -> str | None:
        scheme = urlsplit(ref).scheme.lower()
        if scheme in ("http", "https"):
            return ref
        if scheme or not self._base_url:
            return None
        return f"{self._base_url}/browse/{quote(ref, safe='')}"

    def display_ref(self, ref: str) -> str:
        return _key_of(ref) or ref

    def fetch(self, ref: str) -> TaskInfo | None:
        key = _key_of(ref)
        if key is None or self._base_url is None:
            return None
        token = self._resolve_token()
        if token is None:
            return None
        try:
            response = self._http().get(
                f"{self._base_url}/rest/api/{self._api_version}/issue/{key}",
                params={"fields": "summary,status"},
                headers={"Accept": "application/json", **self._auth_headers(token)},
                auth=self._basic_auth(token),
            )
            response.raise_for_status()
            fields = response.json()["fields"]
            return TaskInfo(
                ref=key,
                title=fields["summary"],
                status=fields["status"]["name"],
                url=self.url_for(ref),
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)
        return self._client

    def _basic_auth(self, token: str) -> httpx.BasicAuth | None:
        if self._auth == "basic" and self._email:
            return httpx.BasicAuth(self._email, token)
        return None

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if self._auth == "bearer" else {}

    def _resolve_token(self) -> str | None:
        with self._token_lock:
            if not self._token_resolved:
                self._token = self._read_token()
                self._token_resolved = True
            return self._token

    def _read_token(self) -> str | None:
        if self._token_command:
            try:
                token = self._run_command(self._token_command).strip()
            except (OSError, subprocess.CalledProcessError):
                token = ""
            if token:
                return token
        return self._env.get(TOKEN_ENV_VAR) or None


def _key_of(ref: str) -> str | None:
    """The issue key in ``ref``: the ref itself, or the last path segment of a
    URL like ``https://x.atlassian.net/browse/ABC-1``."""
    if _KEY_RE.fullmatch(ref):
        return ref
    if urlsplit(ref).scheme.lower() in ("http", "https"):
        last = urlsplit(ref).path.rstrip("/").rsplit("/", 1)[-1]
        if _KEY_RE.fullmatch(last):
            return last
    return None


def _optional_str(value: object) -> str | None:
    return str(value) if value else None


def _argv(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
        raise ValueError("tasks.jira.token_command must be a list of strings (argv)")
    return value
