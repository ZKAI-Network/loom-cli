#!/usr/bin/env python3
"""Local file/shell tools for the Loom thin client.

Exposes the user's project directory as tools so a remote Loom session (the
agent running on the server/sandbox) can read, write, and run commands on the
user's machine. ``loom`` opens a WebSocket tunnel to the server and dispatches
each incoming call to ``_handle_tool_call`` here; a localhost MCP HTTP server
(``start_server``) is also provided for standalone use.

Usage (standalone):
    python3 -m loom_cli.local_tools [--port 0] [--dir .]

Usage (from client.py, over the WebSocket tunnel):
    from . import local_tools as lt
    lt._project_dir = Path("/path/to/project").resolve()
    output = lt._handle_tool_call(tool_name, arguments)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

_server_thread: threading.Thread | None = None
_server_port: int = 0
_shutdown_event: threading.Event = threading.Event()
_project_dir: Path = Path.cwd()

# --- MCP JSON-RPC handler ---


def _resolve_path(rel: str) -> Path:
    """Resolve a relative path against the project directory."""
    resolved = (_project_dir / rel).resolve()
    proj = _project_dir.resolve()
    if not str(resolved).startswith(str(proj)):
        raise ValueError(f"path escapes project directory: {rel}")
    return resolved


def _handle_tools_list() -> list[dict]:
    return [
        {
            "name": "local_read",
            "description": "Read a file from the user's local project directory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to read"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "local_write",
            "description": "Write content to a file in the user's local project directory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to write"},
                    "content": {"type": "string", "description": "File content"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "local_list",
            "description": "List files in a directory of the user's local project.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path (default: .)"},
                },
            },
        },
        {
            "name": "local_shell",
            "description": "Run a shell command in the user's local project directory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)"},
                },
                "required": ["command"],
            },
        },
    ]


def _handle_tool_call(name: str, arguments: dict) -> str:
    try:
        if name == "local_read":
            p = _resolve_path(arguments["path"])
            if not p.is_file():
                return f"Error: file not found: {arguments['path']}"
            return p.read_text(errors="replace")

        if name == "local_write":
            p = _resolve_path(arguments["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(arguments["content"])
            return f"Written {len(arguments['content'])} bytes to {arguments['path']}"

        if name == "local_list":
            rel = arguments.get("path", ".")
            p = _resolve_path(rel)
            if not p.is_dir():
                return f"Error: not a directory: {rel}"
            entries = []
            for child in sorted(p.iterdir()):
                suffix = "/" if child.is_dir() else ""
                size = f"  {child.stat().st_size}B" if child.is_file() else ""
                entries.append(f"{child.name}{suffix}{size}")
            return "\n".join(entries) if entries else "(empty directory)"

        if name == "local_shell":
            cmd = arguments["command"]
            timeout = arguments.get("timeout", 30)
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(_project_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output or "(no output)"

        return f"Error: unknown tool: {name}"
    except Exception as e:
        return f"Error: {e}"


def _handle_request(body: dict) -> dict:
    """Process a single MCP JSON-RPC 2.0 request."""
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "loom-local-tools", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": _handle_tools_list()},
        }

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        output = _handle_tool_call(name, arguments)
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": output}]},
        }

    return {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


# --- HTTP server (aiohttp-free, uses only stdlib) ---


def _serve(port: int) -> None:
    """Start the MCP JSON-RPC HTTP server."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"parse error"}')
                return

            result = _handle_request(body)

            response = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format, *args):
            pass  # suppress logs

    server = HTTPServer(("127.0.0.1", port), Handler)
    global _server_port
    _server_port = server.server_address[1]
    print(f"loom-local-tools: serving on http://127.0.0.1:{_server_port}", file=sys.stderr)
    print(f"loom-local-tools: project dir: {_project_dir}", file=sys.stderr)

    while not _shutdown_event.is_set():
        server.handle_request()
    server.server_close()


def start_server(project_dir: str | Path = ".", port: int = 0) -> int:
    """Start the local tools server in a background thread. Returns the port."""
    global _project_dir, _server_thread, _shutdown_event
    _project_dir = Path(project_dir).resolve()
    _shutdown_event.clear()

    def _run():
        _serve(port)

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()

    # Wait for port to be assigned
    import time
    for _ in range(50):
        if _server_port > 0:
            break
        time.sleep(0.1)
    return _server_port


def stop_server() -> None:
    """Stop the background server."""
    global _server_thread
    _shutdown_event.set()
    if _server_thread:
        _server_thread.join(timeout=2)
        _server_thread = None


def get_mcp_config(port: int) -> dict:
    """Return the MCP server config for registering with a session."""
    return {
        "name": "loom-local-tools",
        "transport": "http",
        "url": f"http://127.0.0.1:{port}/",
        "tools": ["local_read", "local_write", "local_list", "local_shell"],
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Loom local file/shell MCP server")
    parser.add_argument("--port", type=int, default=0, help="Port (0 = random)")
    parser.add_argument("--dir", type=str, default=".", help="Project directory")
    args = parser.parse_args()

    _project_dir = Path(args.dir).resolve()
    print(f"loom-local-tools: project dir: {_project_dir}")
    print(f"loom-local-tools: starting on port {args.port or '(random)'}...")

    try:
        _serve(args.port or 0)
    except KeyboardInterrupt:
        print("\nloom-local-tools: stopped")
