"""A read-only LSP client: a language server runs on a checkout of one
revision and answers hover and definition requests for its files."""

import collections
import itertools
import json
import subprocess
import tarfile
import tempfile
import threading
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from toolbelt import logged_process
from toolbelt.diff_review.revisions import RevisionError, RevisionPair, Side
from toolbelt.ui import api_models as api

#: The Python language server, started with ``uvx`` so nothing is installed.
PYTHON_SERVER = (
    "uvx",
    "--from",
    "basedpyright",
    "basedpyright-langserver",
    "--stdio",
)
REQUEST_TIMEOUT_SECONDS = 120
#: Sessions kept alive (one language server each); the least recently used is
#: stopped to start another.
MAX_SESSIONS = 4
MAX_HEADER_LINES = 20


class LspError(Exception):
    pass


class LspClient:
    def __init__(self, command: Sequence[str], root: Path) -> None:
        self._process = logged_process.Popen(
            list(command),
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._write_lock = threading.Lock()
        self._pending: dict[int, Future[Any]] = {}
        self._pending_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._opened: set[str] = set()
        threading.Thread(target=self._read_messages, daemon=True).start()
        root_uri = root.as_uri()
        self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": "root"}],
                "capabilities": {
                    "workspace": {"configuration": True},
                    "textDocument": {
                        "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "definition": {"linkSupport": True},
                    },
                },
            },
        )
        self.notify("initialized", {})

    def _send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode()
        stdin = self._process.stdin
        assert stdin is not None
        with self._write_lock:
            stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = next(self._ids)
        future: Future[Any] = Future()
        with self._pending_lock:
            self._pending[request_id] = future
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            return future.result(timeout=REQUEST_TIMEOUT_SECONDS)
        except FutureTimeout as err:
            raise LspError(f"{method}: no answer from the language server") from err

    def open_document(self, uri: str, text: str) -> None:
        """Tells the server about a file (once): it answers about open files."""
        if uri in self._opened:
            return
        self._opened.add(uri)
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                }
            },
        )

    def _read_message(self) -> dict[str, Any] | None:
        stdout = self._process.stdout
        assert stdout is not None
        length = None
        for _ in range(MAX_HEADER_LINES):
            line = stdout.readline()
            if not line:
                return None
            header = line.decode().strip()
            if not header:
                break
            name, _, value = header.partition(":")
            if name.lower() == "content-length":
                length = int(value)
        if length is None:
            return None
        body = stdout.read(length)
        if len(body) < length:
            return None
        message: dict[str, Any] = json.loads(body)
        return message

    def _read_messages(self) -> None:
        # Runs for the life of the language server process.
        try:
            while (message := self._read_message()) is not None:
                self._route(message)
        finally:
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for future in pending:
                future.set_exception(LspError("the language server exited"))

    def _route(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is None:
            return  # a notification: progress, diagnostics, logs
        if "method" in message:
            # The server's own request; some servers stall until answered.
            result: Any = None
            if message["method"] == "workspace/configuration":
                result = [None] * len(message["params"]["items"])
            self._send({"jsonrpc": "2.0", "id": message_id, "result": result})
            return
        with self._pending_lock:
            future = self._pending.pop(message_id, None)
        if future is None:
            return
        if "error" in message:
            future.set_exception(LspError(str(message["error"])))
        else:
            future.set_result(message.get("result"))

    def close(self) -> None:
        self._process.kill()
        self._process.wait()


class LspSession:
    """A language server on a checkout of one commit."""

    def __init__(self, command: Sequence[str], repo: Path, commit: str) -> None:
        self._checkout = tempfile.TemporaryDirectory()
        try:
            self.root = Path(self._checkout.name).resolve()
            self._extract(repo, commit)
            self._client = LspClient(command, self.root)
        except BaseException:
            self._checkout.cleanup()
            raise

    def _extract(self, repo: Path, commit: str) -> None:
        with logged_process.Popen(
            ["git", "-C", str(repo), "archive", commit], stdout=subprocess.PIPE
        ) as archive:
            assert archive.stdout is not None
            with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
                tar.extractall(self.root, filter="data")
        if archive.returncode:
            raise RevisionError(f"could not check out {commit}")

    def _open(self, path: str) -> str:
        file = (self.root / path).resolve()
        if not file.is_relative_to(self.root):
            raise LspError(f"outside the checkout: {path}")
        uri = file.as_uri()
        self._client.open_document(uri, file.read_text(errors="replace"))
        return uri

    def _at(self, method: str, path: str, line: int, character: int) -> Any:
        return self._client.request(
            method,
            {
                "textDocument": {"uri": self._open(path)},
                "position": {"line": line, "character": character},
            },
        )

    def hover(self, path: str, line: int, character: int) -> api.ReviewHover:
        result = self._at("textDocument/hover", path, line, character)
        if not result:
            return api.ReviewHover(text=None)
        contents = result["contents"]
        parts = contents if isinstance(contents, list) else [contents]
        texts = [part if isinstance(part, str) else part["value"] for part in parts]
        return api.ReviewHover(text="\n\n".join(texts))

    def definition(self, path: str, line: int, character: int) -> api.ReviewDefinition:
        result = self._at("textDocument/definition", path, line, character)
        locations = result if isinstance(result, list) else [result]
        if not locations or locations[0] is None:
            return api.ReviewDefinition(target=None)
        # A `Location` has `uri` and `range`; a `LocationLink`, `targetUri`
        # and `targetSelectionRange`.
        location = locations[0]
        uri = location.get("targetUri", location.get("uri"))
        start = location.get("targetSelectionRange", location.get("range"))["start"]
        target_path = Path(url2pathname(urlparse(uri).path))
        relative = (
            str(target_path.relative_to(self.root))
            if target_path.is_relative_to(self.root)
            else None
        )
        return api.ReviewDefinition(
            target=api.ReviewDefinitionTarget(
                path=relative,
                uri=uri,
                line=start["line"],
                character=start["character"],
            )
        )

    def close(self) -> None:
        self._client.close()
        self._checkout.cleanup()


class LspSessions:
    """The running sessions, by repo and commit."""

    def __init__(self, command: Sequence[str]) -> None:
        self._command = command
        self._lock = threading.Lock()
        self._sessions: collections.OrderedDict[tuple[Path, str], LspSession] = (
            collections.OrderedDict()
        )

    def session(self, pair: RevisionPair, side: Side) -> LspSession:
        key = (pair.repo, pair.commit(side))
        with self._lock:
            if key in self._sessions:
                self._sessions.move_to_end(key)
                return self._sessions[key]
            session = LspSession(self._command, *key)
            self._sessions[key] = session
            if len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)[1].close()
            return session

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
