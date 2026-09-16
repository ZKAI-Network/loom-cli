"""Server resolution for the ``loom`` CLI.

Precedence (highest first):
  1. an explicit ``--server <url>`` on the command line
  2. the ``LOOM_SERVER`` environment variable
  3. the last server saved by a successful ``loom login``
  4. the built-in default, ``https://loom.mbd.xyz``

Also supports a locally-run server, e.g. ``loom --server http://localhost:6767``
or ``LOOM_SERVER=http://localhost:6767 loom …`` — the same thin CLI targets a
Loom instance running on your own machine identically to the hosted one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_SERVER = "https://loom.mbd.xyz"

from .auth import _state_dir  # reuse the ~/.loom state dir

_DEFAULTS_FILE = "config.json"


def _defaults_path() -> Path:
    return _state_dir() / _DEFAULTS_FILE


def remember_default_server(server: str) -> None:
    """Persist the last successfully-used server as the default target."""
    path = _defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, str] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    if not isinstance(data, dict):
        # A valid but wrong-shaped file (e.g. ``[]``) must not crash login.
        data = {}
    data["server"] = server.rstrip("/")
    path.write_text(json.dumps(data, indent=2))


def _saved_default_server() -> str | None:
    path = _defaults_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    server = data.get("server")
    return server if isinstance(server, str) and server else None


def resolve_server(explicit: str | None) -> str:
    """Resolve the target server URL following the documented precedence."""
    server = explicit or os.environ.get("LOOM_SERVER") or _saved_default_server() or DEFAULT_SERVER
    return server.rstrip("/")
