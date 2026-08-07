"""A minimal language server used to exercise the LSP client for real."""

from __future__ import annotations

import json
import os
import sys
import time

CAPABILITIES = {
    "textDocumentSync": 1,
    "documentSymbolProvider": True,
    "workspaceSymbolProvider": True,
    "definitionProvider": True,
    "referencesProvider": True,
    "hoverProvider": True,
    "typeDefinitionProvider": True,
    "implementationProvider": True,
}

SYMBOL = {
    "name": "parse",
    "kind": 12,
    "location": {
        "uri": "file:///workspace/parser.py",
        "range": {
            "start": {"line": 10, "character": 0},
            "end": {"line": 10, "character": 9},
        },
    },
}

LOCATION = {
    "uri": "file:///workspace/parser.py",
    "range": {
        "start": {"line": 10, "character": 4},
        "end": {"line": 10, "character": 9},
    },
}

REFERENCE = {
    "uri": "file:///workspace/app.py",
    "range": {
        "start": {"line": 3, "character": 8},
        "end": {"line": 3, "character": 13},
    },
}

DIAGNOSTIC = {
    "range": {
        "start": {"line": 2, "character": 0},
        "end": {"line": 2, "character": 5},
    },
    "severity": 1,
    "message": "undefined name 'foo'",
    "source": "fake",
}


def write(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def read_message():
    headers = b""
    while b"\r\n\r\n" not in headers:
        chunk = sys.stdin.buffer.read(1)
        if not chunk:
            return None
        headers += chunk

    length = 0
    for line in headers.decode("ascii").split("\r\n"):
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "content-length":
            length = int(value.strip())

    body = sys.stdin.buffer.read(length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def result_for(method, params):
    if method == "initialize":
        return {"capabilities": CAPABILITIES, "serverInfo": {"name": "fake"}}
    if method == "textDocument/documentSymbol":
        return [SYMBOL]
    if method == "workspace/symbol":
        query = (params or {}).get("query", "")
        return [SYMBOL] if query in SYMBOL["name"] else []
    if method in ("textDocument/definition", "textDocument/typeDefinition",
                  "textDocument/implementation"):
        return [LOCATION]
    if method == "textDocument/references":
        return [LOCATION, REFERENCE]
    if method == "textDocument/hover":
        return {"contents": {"kind": "markdown", "value": "def parse(raw: str)"}}
    if method == "shutdown":
        return None
    return None


def main():
    mode = os.environ.get("FAKE_LSP_MODE", "normal")
    if mode == "crash_on_start":
        sys.stderr.write("fake server refused to start\n")
        sys.stderr.flush()
        return 1

    while True:
        message = read_message()
        if message is None:
            return 0

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "exit":
            return 0

        if method == "textDocument/didOpen":
            write(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": params.get("textDocument", {}).get("uri", ""),
                        "diagnostics": [DIAGNOSTIC],
                    },
                }
            )
            continue

        if request_id is None:
            continue

        if mode == "hang" and method not in ("initialize", "shutdown"):
            time.sleep(30)
            continue

        if mode == "error" and method == "textDocument/definition":
            write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": "internal failure"},
                }
            )
            continue

        write({"jsonrpc": "2.0", "id": request_id, "result": result_for(method, params)})


if __name__ == "__main__":
    sys.exit(main())
