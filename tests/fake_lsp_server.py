"""A language server that answers hover and definition with fixed text, for
the diff review's tests: run as ``python fake_lsp_server.py``."""

import json
import sys


def read() -> dict | None:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        header = line.decode().strip()
        if not header:
            break
        name, _, value = header.partition(":")
        if name.lower() == "content-length":
            length = int(value)
    return json.loads(sys.stdin.buffer.read(length))


def send(message: dict) -> None:
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def main() -> None:
    while (message := read()) is not None:
        method = message.get("method")
        match method:
            case "initialize":
                # The server's own request: the client must answer it.
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1000,
                        "method": "workspace/configuration",
                        "params": {"items": [{}]},
                    }
                )
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"capabilities": {}},
                    }
                )
            case "textDocument/hover":
                position = message["params"]["position"]
                text = f"hover {position['line']}:{position['character']}"
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"contents": {"kind": "plaintext", "value": text}},
                    }
                )
            case "textDocument/definition":
                uri = message["params"]["textDocument"]["uri"]
                start = {"line": 2, "character": 4}
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": [
                            {"uri": uri, "range": {"start": start, "end": start}}
                        ],
                    }
                )
            case "exit":
                return
            case _:
                if "id" in message and "result" not in message:
                    send({"jsonrpc": "2.0", "id": message["id"], "result": None})


main()
