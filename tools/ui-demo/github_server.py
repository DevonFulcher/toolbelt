#!/usr/bin/env python3
"""A fake GitHub API for the `tt ui` demo: the UI's HTTP requests, answered
from the canned data of the fake `gh` next to it.

    python3 tools/ui-demo/github_server.py PORT

`tt ui` talks to GitHub over HTTP (`GithubRequests`), pointed here with
`TT_GITHUB_API_URL`. `POST /graphql` runs the fake `gh api graphql` with the
request's query and variables and returns what it prints, so there is one set
of canned PRs (`SCENARIOS`, `history.json`). REST gets: `repos/<slug>` says the
token is not an admin (no webhooks are managed), and the change gate's conditional
gets always answer "not modified" once they carry the ETag the first answer
gave, so a PR is looked up in full only on the refresher's own schedule.
"""

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GH = Path(__file__).parent / "gh"
ETAG = 'W/"demo"'
HEADERS = {
    "x-ratelimit-limit": "5000",
    "x-ratelimit-remaining": "4999",
    "x-ratelimit-reset": "4102444800",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", **headers: str) -> None:
        self.send_response(status)
        for key, value in {**HEADERS, **headers}.items():
            self.send_header(key.replace("_", "-"), value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/graphql":
            self._send(404)
            return
        args = ["api", "graphql", "-f", f"query={request.get('query', '')}"]
        for key, value in (request.get("variables") or {}).items():
            if value is not None:
                args += ["-f", f"{key}={value}"]
        # Standalone (stdlib only, run by the system python): no toolbelt imports.
        result = subprocess.run(  # noqa: TID251
            [sys.executable, str(GH), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            self._send(404, b'{"message": "Not Found"}')
            return
        self._send(200, result.stdout.encode(), content_type="application/json")

    def do_GET(self) -> None:
        parts = self.path.strip("/").split("/")
        if self.headers.get("if-none-match") == ETAG:
            self._send(304)
        elif parts[0] == "repos" and len(parts) == 3:
            body = json.dumps({"permissions": {"admin": False}}).encode()
            self._send(200, body, content_type="application/json", etag=ETAG)
        else:
            self._send(200, b"[]", content_type="application/json", etag=ETAG)

    def do_DELETE(self) -> None:
        self._send(204)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
