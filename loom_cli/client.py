#!/usr/bin/env python3
"""REST/SSE client backing the ``loom`` CLI's server mode.

Drives a Loom server exactly like the web app does — create a session bound to
the builtin ``loom`` agent, submit a user turn, and stream the reply. Because it
speaks the plain session API, it behaves **identically** wherever the server
runs: only the server URL and whether an auth token exists differ (single-user /
header servers need none; accounts / SSO servers use the bearer token stored by
``loom login``).

    loom --server <url> [-p "…"] [--harness H]

Wire contract (mirrors the web app):
  POST /v1/sessions            {"agent_id": …, "harness_override"?: …}       -> {"id": "conv_…"}
  GET  /v1/sessions/{id}/stream  (Accept: text/event-stream)                 -> SSE
  POST /v1/sessions/{id}/events {"type":"message","data":{"role":"user",...}} -> 202
Stream: accumulate ``response.output_text.delta`` (.delta); stop on a terminal
``response.completed`` / ``.failed`` / ``.incomplete`` / ``.cancelled``.
"""

from __future__ import annotations

import argparse
import collections
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from .auth import load_token

LOOM_AGENT_NAME = "loom"
_STREAM_TIMEOUT = httpx.Timeout(600.0, connect=15.0)  # tool calls hold the stream open

# The chat window's core selectors.
HARNESSES = ["claude-sdk", "openai-agents", "codex", "cursor", "pi", "antigravity", "copilot"]
DEFAULT_HARNESS = "pi"
ALL_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

# Per-engine capability rules — mirror the web app's capability tables so the
# CLI only offers what the selected engine supports.
_EFFORT_VOCAB = {
    "claude-sdk": ["low", "medium", "high", "xhigh", "max"],
    "codex": ["none", "minimal", "low", "medium", "high", "xhigh"],
    "openai-agents": ["none", "minimal", "low", "medium", "high", "xhigh"],
    "copilot": ["low", "medium", "high", "xhigh"],
    "antigravity": ["low", "medium", "high"],
    # pi, cursor → no reasoning-effort control.
}


def effort_levels(harness: str | None) -> list[str] | None:
    return _EFFORT_VOCAB.get(harness or "")


def fast_supported(harness: str | None, model: str | None) -> bool:
    if harness != "claude-sdk":
        return False
    m = (model or "").lower().strip()
    return not m or m.startswith("claude-opus-")  # Opus-only (default is Opus)


def plan_supported(harness: str | None) -> bool:
    return harness == "claude-sdk"  # permission_mode is Claude-only


COMPUTE_TIERS = [
    "cpu-8g", "cpu-16g", "gpu-t4-16g", "gpu-a10-32g",
    "gpu-a100-80g", "gpu-a100-2x", "gpu-a100-4x", "gpu-h100-8x",
]

# Projects: the web files each session under a project via a conversation label
# (defaults to the repo/dir name, else "Scratch"). We mirror that exactly so CLI
# sessions group the same way in the web UI.
PROJECT_LABEL_KEY = "omni_project"
DEFAULT_PROJECT = "Scratch"


def derive_project(workspace: str | None) -> str | None:
    """Repo/dir name from a workspace (local path or ``<url>[#branch]``), matching
    how the web app labels a session; ``None`` when there's no workspace."""
    if not workspace:
        return None
    w = workspace.strip().split("#", 1)[0].rstrip("/")
    if not w:
        return None
    last = w.replace(":", "/").split("/")[-1]
    if last.endswith(".git"):
        last = last[:-4]
    return last or None

# Loom-branded terminal styling (dependency-light ANSI; auto-off when not a TTY
# or when NO_COLOR is set). Pink is Loom's brand accent.
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_PINK = "38;2;244;59;166"


def _c(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _COLOR else s


# Loom brand pink (#E01396) and the "loom" wordmark (ANSI-Shadow block letters) —
# the actual Loom logo.
_LOOM_PINK = "38;2;224;19;150"
_LOOM_WORDMARK = (
    "██╗      ██████╗  ██████╗ ███╗   ███╗",
    "██║     ██╔═══██╗██╔═══██╗████╗ ████║",
    "██║     ██║   ██║██║   ██║██╔████╔██║",
    "███████╗╚██████╔╝╚██████╔╝██║ ╚═╝ ██║",
    "╚══════╝ ╚═════╝  ╚═════╝ ╚═╝     ╚═╝",
)


def print_banner(
    server: str,
    session_id: str,
    harness: str | None,
    project: str | None = None,
    model: str | None = None,
    name: str | None = None,
) -> None:
    """Loom startup banner — the Loom wordmark (brand pink) + this session's
    details, in a box (Claude-Code-style)."""
    short = session_id if len(session_id) <= 20 else session_id[:17] + "…"
    engine = harness or "default"
    if model:
        engine += f" · {model}"
    rows: list[tuple[str, str]] = []
    if name:
        rows.append((f"Welcome back {name}!", "bold"))
    rows += [(w, "logo") for w in _LOOM_WORDMARK]
    rows += [
        ("your data-science agent", "dim"),
        (engine, "dim"),
        (server, "dim"),
        (f"project {project or DEFAULT_PROJECT}  ·  session {short}", "dim"),
    ]
    hint = "type a message · /help for settings · Ctrl-D or /exit to quit"

    if not (_COLOR and sys.stdout.isatty()):
        for text, _ in rows:
            print("  " + text)
        print("  " + hint)
        return

    W = max(len(t) for t, _ in rows)
    run = W + 2
    esc = {"logo": _LOOM_PINK, "bold": "1", "dim": "2"}
    pink = f"\x1b[{_LOOM_PINK}m"
    print(pink + "╭" + "─ loom " + "─" * (run - 7) + "╮" + "\x1b[0m")
    for text, kind in rows:
        pad = W - len(text)
        left, right = pad // 2, pad - pad // 2
        body = f"\x1b[{esc[kind]}m{text}\x1b[0m"
        print(f"{pink}│\x1b[0m " + " " * left + body + " " * right + f" {pink}│\x1b[0m")
    print(pink + "╰" + "─" * run + "╯" + "\x1b[0m")
    print(f"\x1b[2m  {hint}\x1b[0m")


def _auth_headers(server: str) -> dict[str, str]:
    tok = load_token(server)
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _as_list(payload: object) -> list:
    if isinstance(payload, dict):
        data = payload.get("data")
        return data if isinstance(data, list) else []
    return payload if isinstance(payload, list) else []


def _terminal(short: str) -> bool:
    return short in {"completed", "failed", "incomplete", "cancelled", "error"}


def resolve_loom_agent_id(client: httpx.Client, server: str) -> str:
    r = client.get(f"{server}/v1/agents")
    r.raise_for_status()
    loom = next((a for a in _as_list(r.json()) if a.get("name") == LOOM_AGENT_NAME), None)
    if not loom or not loom.get("id"):
        raise SystemExit(f"no '{LOOM_AGENT_NAME}' agent registered on {server}")
    return loom["id"]


def list_hosts(client: httpx.Client, server: str) -> tuple[list, bool]:
    """Return (online external hosts, managed_available). External hosts are
    your own machines/VMs registered with the server; managed = Loom's ephemeral
    sandbox pool (any host with a sandbox_provider)."""
    try:
        hosts = client.get(f"{server}/v1/hosts", timeout=15.0).json().get("hosts", [])
    except Exception:  # noqa: BLE001
        return [], False
    external = [h for h in hosts if h.get("status") == "online" and not h.get("sandbox_provider")]
    managed_available = any(h.get("sandbox_provider") for h in hosts)
    return external, managed_available


def resolve_execution(
    client: httpx.Client,
    server: str,
    *,
    host_type: str | None = None,
    host_id: str | None = None,
    workspace_arg: str | None = None,
    compute: str | None = None,
    exec_timeout_s: int | None = None,
    sandbox_lifetime_s: int | None = None,
    idle_timeout_s: int | None = None,
    aide_models: dict | None = None,
) -> dict:
    """Build the create-body host/workspace fields.

    Honors an explicit ``host_type`` (from the wizard/flags):
    * ``"managed"`` → Loom ephemeral sandbox (server provisions); workspace is an
      optional git repo spec, plus compute tier + runtime budgets.
    * ``"external"`` + ``host_id`` → run on your machine/VM; needs an absolute
      local ``workspace`` (defaults to cwd, like ``claude``).
    When ``host_type`` is None it auto-detects (first online external host, else
    managed) so non-wizard runs still work.
    """
    def _managed_body() -> dict:
        body: dict[str, object] = {"host_type": "managed"}
        if workspace_arg:
            body["workspace"] = workspace_arg  # repo spec for managed
        if compute:
            body["compute"] = compute
        # Clamp to the API's second bounds so a too-small value can't 422.
        # Guard on ``is not None`` (not truthiness) so an explicit 0 is clamped to
        # the documented minimum rather than silently dropped to the server default.
        if exec_timeout_s is not None:
            body["exec_timeout_s"] = _clamp(exec_timeout_s, 30, 86400)
        if sandbox_lifetime_s is not None:
            body["sandbox_lifetime_s"] = _clamp(sandbox_lifetime_s, 300, 86400)
        if idle_timeout_s is not None:
            body["idle_timeout_s"] = _clamp(idle_timeout_s, 60, 86400)
        for k, v in (aide_models or {}).items():
            if v:
                body[k] = v
        return body

    def _external_body(hid: str) -> dict:
        ws = workspace_arg or os.getcwd()
        if not ws.startswith("/"):
            ws = os.path.abspath(ws)
        return {"host_id": hid, "host_type": "external", "workspace": ws}

    # Explicit choice from the wizard/flags.
    if host_type == "managed":
        return _managed_body()
    if host_type == "external" and host_id:
        return _external_body(host_id)

    # Auto-detect: a specific --host, else the first online external, else managed.
    external, _ = list_hosts(client, server)
    if host_id:
        return _external_body(host_id)
    if external:
        return _external_body(external[0]["host_id"])
    return _managed_body()


def _to_int(s: str | None) -> int | None:
    try:
        return int(s) if s not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _min_to_sec(minutes: str | None, lo: int, hi: int) -> int | None:
    """Minutes string → whole seconds, clamped to [lo, hi] (mirrors the web's
    clampSeconds). Blank → None (leave the deploy default)."""
    if not minutes:
        return None
    try:
        sec = round(float(minutes) * 60)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, sec))


def _clamp(v: int | None, lo: int, hi: int) -> int | None:
    return None if v is None else max(lo, min(hi, v))


def aide_model_options(client: httpx.Client, server: str, agent_id: str) -> list[str]:
    """Models the AIDE engine can use — the union of the codex/claude/openai
    catalogs (matches the web's merged AIDE model picker)."""
    seen: list[str] = []
    for h in ("codex", "claude-sdk", "openai-agents"):
        for m in list_models(client, server, agent_id, h):
            if m not in seen:
                seen.append(m)
    return seen


def create_session(
    client: httpx.Client,
    server: str,
    agent_id: str,
    *,
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    fast: bool = False,
    plan: bool = False,
    execution: dict | None = None,
) -> str:
    body: dict[str, object] = {"agent_id": agent_id, "initial_items": []}
    if harness:
        body["harness_override"] = harness
    if model:
        body["model_override"] = model
    if effort:
        body["reasoning_effort"] = effort
    if fast:
        body["fast_mode"] = True
    if plan:
        body["permission_mode"] = "plan"
    if execution:
        body.update(execution)
    r = client.post(f"{server}/v1/sessions", json=body)
    r.raise_for_status()
    return r.json()["id"]


def set_project(client: httpx.Client, server: str, session_id: str, name: str) -> None:
    """File a session under a project (or move it): PATCH the ``omni_project``
    label. An empty name removes it from its project (server deletes the row)."""
    r = client.patch(
        f"{server}/v1/sessions/{session_id}", json={"labels": {PROJECT_LABEL_KEY: name}}
    )
    r.raise_for_status()


def _conn_get(client: httpx.Client, server: str, path: str, **params) -> dict:
    r = client.get(f"{server}{path}", params={k: v for k, v in params.items() if v}, timeout=25.0)
    if r.status_code != 200:
        try:
            msg = r.json().get("error") or r.text[:200]
        except Exception:  # noqa: BLE001
            msg = r.text[:200]
        raise RuntimeError(msg)
    return r.json()


def connections_status(client: httpx.Client, server: str) -> object:
    """GET /v1/connections — data connections (Google/GitHub/HF/Kaggle) + status."""
    return _conn_get(client, server, "/v1/connections")


def pick_bigquery(client: httpx.Client, server: str) -> str | None:
    """Browse the connected Google account's BigQuery and return project.dataset.table."""
    d = _conn_get(client, server, "/v1/connections/google/bigquery")
    project, datasets = d.get("project"), d.get("datasets") or []
    if not datasets:
        print(_c("2", "  no datasets found for this connection"))
        return None
    ds = _select("BigQuery dataset", datasets, datasets[0])
    if not ds:
        return None
    t = _conn_get(client, server, "/v1/connections/google/bigquery", dataset=ds)
    tables = [x.get("id") if isinstance(x, dict) else x for x in (t.get("tables") or [])]
    if not tables:
        return f"{project}.{ds}"
    tbl = _select("BigQuery table", tables, tables[0])
    return f"{project}.{ds}.{tbl}" if tbl else f"{project}.{ds}"


def pick_gcs(client: httpx.Client, server: str) -> str | None:
    """Browse the connected Google account's GCS and return bucket[/object-or-prefix]."""
    d = _conn_get(client, server, "/v1/connections/google/gcs")
    buckets = d.get("buckets") or []
    if not buckets:
        print(_c("2", "  no buckets found for this connection"))
        return None
    b = _select("GCS bucket", buckets, d.get("default_bucket") or buckets[0])
    if not b:
        return None
    o = _conn_get(client, server, "/v1/connections/google/gcs", bucket=b)
    objs = [x.get("id") if isinstance(x, dict) else x for x in (o.get("objects") or [])]
    pick = _select("GCS object/folder", ["(whole bucket)"] + objs, "(whole bucket)")
    return b if not pick or pick == "(whole bucket)" else f"{b}/{pick}"


def source_directives(bigquery: str | None, gcs: str | None) -> str:
    """The leading directive(s) the web prepends to the first message for a picked
    data source, so the agent materializes it before acting."""
    out = ""
    if bigquery:
        out += (
            f"Data source — BigQuery table `{bigquery}`: materialize it into data/ "
            "(sample if large), then continue.\n\n"
        )
    if gcs:
        out += (
            f"Data source — Cloud Storage `{gcs}`: download it into data/ (all objects "
            "under the prefix; sample if large), then continue.\n\n"
        )
    return out


def _slug(text: str, maxlen: int = 48) -> str:
    s = "".join(c.lower() if c.isalnum() else "-" for c in text)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:maxlen].strip("-") or "plan"


def write_plan_file(task: str, text: str) -> str | None:
    """Save a produced plan to ~/.claude/plans/<slug>.md (matches the web's plan
    folder; claude-sdk returns the plan in-conversation rather than writing one).

    Returns ``None`` (instead of raising) if the plan can't be written, so a
    read-only home / full disk never terminates the caller."""
    if not text.strip():
        return None
    try:
        import hashlib
        d = os.path.expanduser("~/.claude/plans")
        os.makedirs(d, exist_ok=True)
        # Append a short hash of the full task so two tasks that share a 48-char
        # slug prefix don't overwrite each other's saved plan.
        digest = hashlib.sha1(task.encode("utf-8")).hexdigest()[:6]
        path = os.path.join(d, f"{_slug(task)}-{digest}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Plan: {task}\n\n{text.strip()}\n")
        return path
    except OSError as e:
        print(f"(could not save plan file: {e})", file=sys.stderr)
        return None


def list_models(client: httpx.Client, server: str, agent_id: str, harness: str) -> list[str]:
    try:
        r = client.get(
            f"{server}/v1/agents/{agent_id}/models", params={"harness": harness}, timeout=20.0
        )
        r.raise_for_status()
        return [m["id"] for m in r.json().get("models", []) if m.get("id")]
    except Exception:  # noqa: BLE001
        return []


def set_harness(
    client: httpx.Client,
    server: str,
    session_id: str,
    *,
    harness: str,
    model: str | None = None,
    effort: str | None = None,
) -> None:
    """Change engine/model/effort mid-session (idle only): POST …/set-harness."""
    body: dict[str, object] = {"harness_override": harness}
    if model:
        body["model_override"] = model
    if effort:
        body["reasoning_effort"] = effort
    r = client.post(f"{server}/v1/sessions/{session_id}/set-harness", json=body)
    r.raise_for_status()


def update_session(client: httpx.Client, server: str, session_id: str, **fields) -> None:
    """Update mid-session settings (fast_mode, permission_mode, …): PATCH …/sessions/{id}."""
    r = client.patch(f"{server}/v1/sessions/{session_id}", json=fields)
    r.raise_for_status()


def _select_toggle(title: str, default: bool) -> bool:
    return _select(title, ["off", "on"], "on" if default else "off") == "on"


def _prompt_text(title: str, default: str | None) -> str | None:
    """Free-text prompt (Enter keeps the default). Non-TTY → default."""
    if not sys.stdin.isatty():
        return default
    hint = f" [{default}]" if default else " [Enter to skip]"
    raw = input(_c("1;" + _PINK, f"  {title}{hint}: ")).strip()
    return raw or default


def _opt_label(o: str, *, default: str | None, selected: bool) -> str:
    # "(default)" after the default option's name (skip if it already says default).
    suffix = " (default)" if (o == default and "default" not in o.lower()) else ""
    body = o + _c("2", suffix)
    return _c("1;" + _PINK, "❯ ") + _c("1", body) if selected else "  " + body


def _prompt_number(title: str, options: list[str], default: str | None) -> str | None:
    """Typed fallback picker (when arrow-key raw mode isn't available)."""
    print(_c("1;" + _PINK, f"\n{title}"))
    for i, o in enumerate(options, 1):
        print(f"  {i}. {_opt_label(o, default=default, selected=False).strip()}")
    hint = f" [{default}]" if default else " [Enter to skip]"
    while True:
        raw = input(_c("1;" + _PINK, f"  choose{hint}: ")).strip()
        if raw == "":
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        if raw in options:
            return raw
        print(_c("2", "  (use ↑/↓ + Enter, or type a number/name)"))


def _select(title: str, options: list[str], default: str | None = None) -> str | None:
    """Arrow-key menu: ↑/↓ (or j/k) to move, Enter to choose. Falls back to a
    typed picker when there's no interactive TTY."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return default
    try:
        import termios
        import tty
    except Exception:  # noqa: BLE001 — non-Unix: fall back to typing
        return _prompt_number(title, options, default)

    idx = options.index(default) if default in options else 0
    n = len(options)

    def render(first: bool) -> None:
        if not first:
            sys.stdout.write(f"\x1b[{n}A")  # move cursor back up over the list
        for i in range(n):
            sys.stdout.write("\x1b[2K" + _opt_label(options[i], default=default, selected=i == idx) + "\r\n")
        sys.stdout.flush()

    print(_c("1;" + _PINK, title) + _c("2", "   ↑/↓ then Enter"))
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        render(True)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                break
            if ch in ("\x03", "\x04", "q"):  # Ctrl-C / Ctrl-D / q → keep default
                idx = options.index(default) if default in options else idx
                break
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                if seq == "[A":
                    idx = (idx - 1) % n
                elif seq == "[B":
                    idx = (idx + 1) % n
            elif ch == "k":
                idx = (idx - 1) % n
            elif ch == "j":
                idx = (idx + 1) % n
            render(False)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return options[idx]


# Per-user CLI defaults: the start-of-session answers are remembered here so we
# ask them once, not every session. Compute (and the tier, when ephemeral) are
# the exception — always asked. Mid-session `/command` changes update this too.
_DEFAULTS_PATH = os.path.expanduser("~/.config/loom/cli-defaults.json")
# What persists as a default. NOT compute/host (always asked) or project
# (derived per workspace).
_DEFAULT_KEYS = (
    "harness", "model", "effort", "fast",
    "exec_timeout", "lifetime", "idle_timeout",
    "aide_code", "aide_feedback", "aide_report",
    "bigquery", "gcs", "show_reasoning",
)


def load_all_defaults() -> dict:
    """The whole defaults file: ``{server_url: {settings}}``. A pre-per-server
    flat file is folded under a shared ``_legacy`` bucket so nothing is lost."""
    try:
        with open(_DEFAULTS_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError):
        # Missing, unreadable, or a directory — treat as "no defaults" rather
        # than crashing every CLI invocation on the optional defaults file.
        return {}
    if not isinstance(d, dict):
        return {}
    # Legacy flat file (top-level settings, not per-server buckets).
    if "_configured" in d or any(k in d for k in _DEFAULT_KEYS):
        return {"_legacy": d}
    return d


def server_defaults(server: str) -> dict:
    """Saved defaults for ``server`` — its own bucket, else the ``_legacy``
    fallback so a returning user's prefs pre-fill the first prompt on a new
    server."""
    alld = load_all_defaults()
    return alld.get(server) or alld.get("_legacy") or {}


def defaults_configured(server: str) -> bool:
    """True once THIS server has its own saved bucket (so we stop asking)."""
    return server in load_all_defaults()


def save_defaults(server: str, cfg: dict) -> None:
    """Persist ``_DEFAULT_KEYS`` from ``cfg`` under ``server``'s bucket, so each
    server (prod / local / any VM) keeps its own defaults."""
    alld = load_all_defaults()
    bucket = dict(alld.get(server) or {})
    for k in _DEFAULT_KEYS:
        if k in cfg:
            bucket[k] = cfg[k]
    bucket["_configured"] = True
    alld[server] = bucket
    try:
        os.makedirs(os.path.dirname(_DEFAULTS_PATH), exist_ok=True)
        tmp = _DEFAULTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(alld, fh, indent=2)
        os.replace(tmp, _DEFAULTS_PATH)
    except OSError:
        pass


def apply_defaults(cfg: dict, defaults: dict) -> dict:
    """Fill ``cfg`` fields that weren't set by an explicit flag from saved
    defaults, so a returning user's engine/model/… carry over without prompting."""
    for k in _DEFAULT_KEYS:
        if k not in defaults:
            continue
        cur = cfg.get(k)
        # Unset = None, or fast/show_reasoning left at their falsy/omitted state.
        if cur is None:
            cfg[k] = defaults[k]
    return cfg


def run_wizard(client: httpx.Client, server: str, agent_id: str, cfg: dict, *,
               ask_all: bool = True) -> dict:
    """Ask for the session settings at start (↑/↓ + Enter; Enter = default),
    showing only what the selected engine supports. When ``ask_all`` is False
    (the user already has saved defaults) only the compute **tier** is asked —
    everything else uses the saved defaults. Fast mode is offered when
    supported; Plan mode is not asked here (it's a trigger via /plan)."""
    if not sys.stdin.isatty():
        cfg["harness"] = cfg.get("harness") or DEFAULT_HARNESS
        return cfg
    if not ask_all:
        # Returning user: keep saved engine/model/effort/fast/customize/data
        # source; only the compute tier (below) is asked each session.
        cfg["harness"] = cfg.get("harness") or DEFAULT_HARNESS
        if not effort_levels(cfg["harness"]):
            cfg["effort"] = None
        if cfg.get("host_type") == "managed":
            chosen = _select(
                "Compute tier", ["(server default)"] + COMPUTE_TIERS,
                cfg.get("compute") if cfg.get("compute") in COMPUTE_TIERS else "(server default)",
            )
            cfg["compute"] = None if chosen == "(server default)" else chosen
        return cfg
    print(_c("2", "Configure this session — ↑/↓ then Enter (saved as your defaults):"))
    cfg["harness"] = _select(
        "Engine", HARNESSES, cfg.get("harness") or DEFAULT_HARNESS
    ) or DEFAULT_HARNESS

    models = list_models(client, server, agent_id, cfg["harness"])
    if models:
        chosen = _select(
            "Model", ["(server default)"] + models,
            cfg.get("model") if cfg.get("model") in models else "(server default)",
        )
        cfg["model"] = None if chosen == "(server default)" else chosen

    # Reasoning effort — only for engines that support it, with that engine's vocab.
    levels = effort_levels(cfg["harness"])
    if levels:
        chosen = _select(
            "Reasoning effort", ["(server default)"] + levels,
            cfg.get("effort") if cfg.get("effort") in levels else "(server default)",
        )
        cfg["effort"] = None if chosen == "(server default)" else chosen
    else:
        cfg["effort"] = None  # unsupported by this engine

    # Fast mode — offered when the engine supports it (Claude Opus). Plan mode is
    # NOT asked here: it's a trigger via /plan.
    if fast_supported(cfg["harness"], cfg.get("model")):
        cfg["fast"] = _select_toggle("Fast mode", bool(cfg.get("fast")))

    # Compute tier + runtime/AIDE (the CPU·8GB popover) — only when running on
    # Loom ephemeral compute (chosen up front by the compute picker).
    if cfg.get("host_type") == "managed":
        chosen = _select(
            "Compute tier", ["(server default)"] + COMPUTE_TIERS,
            cfg.get("compute") if cfg.get("compute") in COMPUTE_TIERS else "(server default)",
        )
        cfg["compute"] = None if chosen == "(server default)" else chosen
        already = any(cfg.get(k) for k in ("exec_timeout", "lifetime", "idle_timeout",
                                           "aide_code", "aide_feedback", "aide_report"))
        if _select_toggle("Customize runtime & AIDE models", already):
            # Timeouts are entered in MINUTES (like the web) and converted+clamped
            # to the API's second bounds so an out-of-range value can't 422.
            cfg["exec_timeout"] = _min_to_sec(_prompt_text("exec timeout (minutes)", "5"), 30, 86400)
            cfg["lifetime"] = _min_to_sec(_prompt_text("sandbox lifetime (minutes)", "1440"), 300, 86400)
            cfg["idle_timeout"] = _min_to_sec(_prompt_text("idle timeout (minutes, blank=none)", ""), 60, 86400)
            aide_opts = ["(skill default)"] + aide_model_options(client, server, agent_id)
            for key, label in (("aide_code", "AIDE code model"),
                               ("aide_feedback", "AIDE feedback model"),
                               ("aide_report", "AIDE report model")):
                pick = _select(label, aide_opts, cfg.get(key) if cfg.get(key) in aide_opts else "(skill default)")
                cfg[key] = None if not pick or pick == "(skill default)" else pick

    # Optional data source (BigQuery / Cloud Storage) — needs a Google connection.
    if _select_toggle("Attach a data source? (BigQuery / Cloud Storage)",
                      bool(cfg.get("bigquery") or cfg.get("gcs"))):
        kind = _select("Data source", ["BigQuery", "Cloud Storage", "both"], "BigQuery")
        try:
            if kind in ("BigQuery", "both"):
                cfg["bigquery"] = pick_bigquery(client, server) or cfg.get("bigquery")
            if kind in ("Cloud Storage", "both"):
                cfg["gcs"] = pick_gcs(client, server) or cfg.get("gcs")
        except Exception as exc:  # noqa: BLE001
            print(_c("2", f"  data source unavailable: {exc}"))
    return cfg


def _print_commands() -> None:
    print(_c("1;" + _LOOM_PINK, "commands"))
    width = max(len(f"{c} {h}".strip()) for c, h, _ in _COMMAND_HELP)
    for cmd, hint, desc in _COMMAND_HELP:
        sig = f"{cmd} {hint}".strip()
        print(f"  {_c(_LOOM_PINK, sig.ljust(width))}   {_c('2', desc)}")


def handle_command(client: httpx.Client, server: str, agent_id: str, state: dict, text: str) -> bool:
    """Handle a /slash command. Returns False to exit the REPL, else True."""
    parts = text.split()
    cmd, arg = parts[0], (parts[1] if len(parts) > 1 else None)
    sid = state["session_id"]

    def _apply(**kw) -> None:
        try:
            set_harness(client, server, sid, harness=state["harness"], **kw)
            state.update(kw)
            print(_c("2", "  → updated"))
        except httpx.HTTPStatusError as exc:
            print(_c("2", f"  failed: {exc.response.status_code} {exc.response.text[:120]}"))
        except Exception as exc:  # noqa: BLE001
            print(_c("2", f"  failed: {exc}"))

    if cmd in ("/exit", "/quit"):
        return False
    if cmd == "/help":
        _print_commands()
    elif cmd == "/skills":
        skills = list_skills(client, server, sid)
        state["_skills"] = skills
        if not skills:
            print("  no skills resolved yet (the runner may still be binding — try again shortly)")
        else:
            print("  skills (invoke with /<name> [args], like the web):")
            for n in sorted(skills):
                desc = (skills[n] or "").strip().splitlines()[0][:80] if skills[n] else ""
                print(f"    /{n}" + (f"  — {desc}" if desc else ""))
    elif cmd == "/settings":
        h = state["harness"]
        onoff = lambda b: "on" if b else "off"
        eff = (state["effort"] or "(default)") if effort_levels(h) else "n/a"
        fast = onoff(state.get("fast")) if fast_supported(h, state.get("model")) else "n/a"
        plan = "use /plan <task>" if plan_supported(h) else "n/a"
        host = ("Loom ephemeral compute" if state.get("host_type") == "managed"
                else (state.get("host_id") or "auto"))
        print(
            f"  server:  {server}\n  engine:  {h}\n"
            f"  model:   {state['model'] or '(default)'}\n"
            f"  effort:  {eff}\n  fast:    {fast}\n  plan:    {plan}\n"
            f"  host:    {host}\n  compute: {state.get('compute') or '(default)'}\n"
            f"  project: {state.get('project') or DEFAULT_PROJECT}\n  session: {sid}"
        )
    elif cmd == "/fast":
        if not fast_supported(state["harness"], state.get("model")):
            print(f"  fast mode isn't available for {state['harness']} (Claude Opus only)")
        else:
            want = (arg == "on") if arg in ("on", "off") else not state.get("fast")
            try:
                update_session(client, server, sid, fast_mode=want)
                state["fast"] = want
                print(_c("2", f"  → fast {'on' if want else 'off'}"))
            except Exception as exc:  # noqa: BLE001
                print(_c("2", f"  failed: {exc}"))
    elif cmd == "/compute":
        print(f"  compute: {state.get('compute') or '(server default)'}  (set at start: --compute <tier>)")
    elif cmd in ("/datasource", "/storage"):
        try:
            src = pick_bigquery(client, server) if cmd == "/datasource" else pick_gcs(client, server)
        except Exception as exc:  # noqa: BLE001
            print(_c("2", f"  data source unavailable: {exc}"))
            src = None
        if src:
            key = "bigquery" if cmd == "/datasource" else "gcs"
            state[key] = src
            state["pending_directive"] = state.get("pending_directive", "") + source_directives(
                src if key == "bigquery" else None, src if key == "gcs" else None
            )
            print(_c("2", f"  → attached {key}: {src} (used on your next message)"))
    elif cmd == "/connections":
        try:
            print("  " + json.dumps(connections_status(client, server))[:500])
        except Exception as exc:  # noqa: BLE001
            print(_c("2", f"  failed: {exc}"))
    elif cmd == "/share":
        try:
            response = client.post(f"{server}/v1/sessions/{sid}/pi-share", timeout=75.0)
            response.raise_for_status()
            url = response.json()["url"]
            print(_c("2", f"  → Pi share: {url}"))
        except httpx.HTTPStatusError as exc:
            print(_c("2", f"  failed: {exc.response.status_code} {exc.response.text[:200]}"))
        except Exception as exc:  # noqa: BLE001
            print(_c("2", f"  failed: {exc}"))
    elif cmd == "/project":
        if arg is None:
            print(f"  project: {state.get('project') or DEFAULT_PROJECT}  (/project <name> to move, /project \"\" to remove)")
        else:
            name = text.split(maxsplit=1)[1].strip().strip('"') if len(parts) > 1 else ""
            try:
                set_project(client, server, sid, name)
                state["project"] = name or None
                print(_c("2", f"  → {'removed from project' if not name else 'moved to project ' + name}"))
            except Exception as exc:  # noqa: BLE001
                print(_c("2", f"  failed: {exc}"))
    elif cmd == "/engine":
        if not arg:
            print("  engines: " + ", ".join(HARNESSES))
        elif arg not in HARNESSES:
            print(f"  unknown engine '{arg}'")
        else:
            try:
                set_harness(client, server, sid, harness=arg)
                state.update(harness=arg, model=None, effort=None)
                print(_c("2", f"  → engine set to {arg} (model/effort reset to default)"))
            except Exception as exc:  # noqa: BLE001
                print(_c("2", f"  failed: {exc}"))
    elif cmd == "/model":
        models = list_models(client, server, agent_id, state["harness"])
        if not arg:
            print("  models: " + (", ".join(models) if models else "(none listed)"))
        else:
            _apply(model=arg, effort=state["effort"])
    elif cmd == "/effort":
        levels = effort_levels(state["harness"])
        if not levels:
            print(f"  reasoning effort isn't supported by {state['harness']}")
        elif not arg:
            print("  levels: " + ", ".join(levels))
        elif arg not in levels:
            print(f"  '{arg}' not valid for {state['harness']} ({', '.join(levels)})")
        else:
            _apply(model=state["model"], effort=arg)
    elif cmd in ("/attach", "/image"):
        path = text.split(maxsplit=1)[1].strip() if len(parts) > 1 else ""
        if not path:
            print(f"  usage: {cmd} <path>  — sent with your next message")
        else:
            part = upload_attachment(client, server, sid, path)
            if part:
                state.setdefault("pending_files", []).append(part)
    elif cmd == "/title":
        name = text.split(maxsplit=1)[1].strip() if len(parts) > 1 else ""
        if not name:
            print("  usage: /title <name>")
        else:
            try:
                patch_session(client, server, sid, title=name)
                print(_c("2", f"  → renamed to {name!r}"))
            except Exception as exc:  # noqa: BLE001
                print(_c("2", f"  failed: {exc}"))
    elif cmd == "/archive":
        try:
            patch_session(client, server, sid, archived=True)
            print(_c("2", "  → archived (still usable; hidden from the active list)"))
        except Exception as exc:  # noqa: BLE001
            print(_c("2", f"  failed: {exc}"))
    elif cmd == "/usage":
        try:
            d = client.get(f"{server}/v1/sessions/{sid}", timeout=15.0).json()
            foot = _usage_footer({
                "context_tokens": d.get("last_total_tokens"),
                "context_window": d.get("context_window"),
                "total_cost_usd": d.get("total_cost_usd"),
            })
            print("  " + (foot or "no usage recorded yet"))
        except Exception as exc:  # noqa: BLE001
            print(_c("2", f"  failed: {exc}"))
    elif cmd == "/todos":
        try:
            d = client.get(f"{server}/v1/sessions/{sid}", timeout=15.0).json()
            items = d.get("todos") or []
            if not items:
                print("  (no todos)")
            for t in items:
                mark = {"completed": "✓", "in_progress": "▸"}.get(str(t.get("status")), "○")
                print(f"  {mark} {t.get('content') or ''}")
        except Exception as exc:  # noqa: BLE001
            print(_c("2", f"  failed: {exc}"))
    elif cmd == "/reasoning":
        want = (arg == "on") if arg in ("on", "off") else not state.get("show_reasoning", True)
        state["show_reasoning"] = want
        print(_c("2", f"  → reasoning display {'on' if want else 'off'}"))
    elif cmd == "/sessions":
        try:
            rows = list_sessions(client, server)
            print(_c("1;" + _LOOM_PINK, "  your recent sessions") + _c("2", "  (resume: loom --resume)"))
            for s in rows:
                mark = "→ " if s.get("id") == sid else "  "
                print(f"  {mark}{_session_row(s)}")
        except Exception as exc:  # noqa: BLE001
            print(_c("2", f"  failed: {exc}"))
    else:
        print(f"  unknown command {cmd} (try /help)")
    # A change to a persisted setting updates this server's saved defaults too.
    if cmd in ("/engine", "/model", "/effort", "/fast", "/datasource", "/storage", "/reasoning"):
        save_defaults(server, state)
    return True


def wait_until_live(client: httpx.Client, server: str, session_id: str, timeout: float = 600.0) -> bool:
    """Poll until the session has an online runner (creating a session with a
    host launches one; managed sandboxes take longer to cold-start). Prints
    status transitions to stderr so a cold start isn't a silent wait."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            d = client.get(f"{server}/v1/sessions/{session_id}", timeout=15.0).json()
        except Exception:  # noqa: BLE001
            d = {}
        if d.get("runner_online"):
            return True
        st = d.get("status")
        if st and st != last:
            last = st
            print(f"[{st}…]", file=sys.stderr, flush=True)
        time.sleep(2.0)
    return False


def _post_event(server: str, session_id: str, body: dict) -> None:
    # A separate short-lived client so the POST can run while the main thread
    # holds the streaming response (avoids sharing one connection concurrently).
    # Generous read timeout: on a cold managed sandbox the submit can block while
    # the runner provisions.
    with httpx.Client(headers=_auth_headers(server), timeout=httpx.Timeout(120.0, connect=15.0)) as c:
        r = c.post(f"{server}/v1/sessions/{session_id}/events", json=body)
        if r.status_code == 503:
            raise RuntimeError(
                "the server has no runner bound to this session (503). On a bare "
                "server nothing auto-launches one; use a deployment that provisions "
                "a managed runner/host, or point --server at a fully running server."
            )
        r.raise_for_status()


def _post_message(server: str, session_id: str, text: str) -> None:
    _post_event(server, session_id, {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    })


def _skill_event(name: str, arguments: str) -> dict:
    """The slash-command wire shape: invoke skill ``name`` with ``arguments``
    (everything after the ``/name`` token). The server resolves and runs the
    skill server-side, exactly as the web composer does."""
    return {"type": "slash_command",
            "data": {"kind": "skill", "name": name, "arguments": arguments}}


def list_skills(client: httpx.Client, server: str, session_id: str) -> dict:
    """Skills available to this session — bundled + runner-discovered — read from
    the session snapshot's ``skills`` field (the same source the web's slash menu
    uses). Runner skills resolve asynchronously (nudged by ``session.skills``
    events), so callers re-read on a miss. Returns ``{name: description}``."""
    try:
        d = client.get(f"{server}/v1/sessions/{session_id}", timeout=15.0).json()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for s in d.get("skills") or []:
        if isinstance(s, dict) and s.get("name"):
            out[str(s["name"])] = str(s.get("description") or "")
    return out


def upload_attachment(client: httpx.Client, server: str, session_id: str, path: str) -> dict | None:
    """Upload a local file to the session's file store (POST .../resources/files,
    multipart field ``file``) and return the message content part that references
    it — ``input_image`` for images, else ``input_file`` (mirrors the web
    composer). Returns None on a missing file / failed upload."""
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        print(_c("2", f"  no such file: {path}"), file=sys.stderr)
        return None
    name = os.path.basename(p)
    ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
    try:
        with open(p, "rb") as fh:
            r = client.post(
                f"{server}/v1/sessions/{session_id}/resources/files",
                files={"file": (name, fh, ctype)}, timeout=120.0,
            )
        r.raise_for_status()
        fid = r.json().get("id")
    except httpx.HTTPStatusError as exc:
        print(_c("2", f"  upload failed: {exc.response.status_code} {exc.response.text[:120]}"),
              file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001
        print(_c("2", f"  upload failed: {exc}"), file=sys.stderr)
        return None
    if not fid:
        return None
    kind = "input_image" if ctype.startswith("image/") else "input_file"
    print(_c("2", f"  attached {name} ({kind.split('_')[1]})"))
    return {"type": kind, "file_id": fid, "filename": name}


def list_sessions(client: httpx.Client, server: str, *, limit: int = 30) -> list[dict]:
    """Recent sessions (GET /v1/sessions, newest first) — the CLI equivalent of
    the web sidebar. Project lives under labels['omni_project']."""
    r = client.get(
        f"{server}/v1/sessions",
        params={"limit": limit, "order": "desc", "sort_by": "updated_at"},
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json().get("data") or []


def _session_row(s: dict) -> str:
    labels = s.get("labels") or {}
    proj = labels.get(PROJECT_LABEL_KEY) or "—"
    title = (s.get("title") or "(untitled)").strip()
    status = s.get("status") or ""
    return f"{s.get('id',''):<24}  {proj:<16.16}  {status:<8}  {title:.50}"


def patch_session(client: httpx.Client, server: str, session_id: str, **fields) -> None:
    """PATCH /v1/sessions/{id} — set title, archived, etc."""
    client.patch(f"{server}/v1/sessions/{session_id}", json=fields).raise_for_status()


def _interrupt(server: str, session_id: str) -> None:
    """Cancel the in-flight turn (POST events {type:'interrupt'}) — same path the
    web's stop button uses; the server emits session.interrupted + incomplete."""
    with httpx.Client(headers=_auth_headers(server), timeout=15.0) as c:
        c.post(f"{server}/v1/sessions/{session_id}/events",
               json={"type": "interrupt", "data": {}}).raise_for_status()


def _resolve_elicitation(
    server: str, session_id: str, eid: str, action: str, content: dict | None = None
) -> None:
    """Answer a tool-approval / elicitation request:
    POST /v1/sessions/{id}/elicitations/{eid}/resolve  {"action", "content"?}.
    content carries the web's richer verdicts: {"remember": true} (don't ask
    again for this tool), {"bypass_all": true} (approve everything this session),
    {"allow_all_edits": true} (edit tools)."""
    body: dict[str, object] = {"action": action}
    if content:
        body["content"] = content
    with httpx.Client(headers=_auth_headers(server), timeout=30.0) as c:
        c.post(
            f"{server}/v1/sessions/{session_id}/elicitations/{eid}/resolve", json=body
        ).raise_for_status()


def _decide_approval(params: dict, *, auto_approve: bool, approvals: dict | None = None):
    """Show an approval request and return ``(action, content)`` — the same set of
    verdicts as the web's ApprovalCard: Approve / Approve & don't ask again for
    the tool / Approve & bypass all this session / (allow all edits) / Reject.

    ``approvals`` is session-scoped memory (shared across turns AND across the
    lead + every spawned worker agent, since the server mirrors worker prompts
    onto this stream). Once the user picks *bypass all* or *don't ask again for
    <tool>*, that choice is remembered and auto-applied to every later prompt —
    so a multi-agent run stops re-prompting for each worker.

    ``--yes`` → accept; non-TTY → decline (keeps scripted runs non-blocking)."""
    msg = str(params.get("message") or "loom is requesting approval")
    command, cwd = params.get("command"), params.get("cwd")
    scope = params.get("remember_scope") if isinstance(params.get("remember_scope"), dict) else {}
    tool = (scope or {}).get("host") or (scope or {}).get("tool")
    allow_edits = params.get("allow_all_edits") is True

    # Honor a remembered choice without re-prompting (covers worker agents).
    if approvals is not None:
        if approvals.get("bypass_all"):
            return "accept", {"bypass_all": True}
        if tool and tool in approvals.get("remembered", set()):
            return "accept", {"remember": True}

    sys.stderr.write(_c("1;" + _PINK, "\n● approval needed") + f"\n  {msg}\n")
    if command:
        sys.stderr.write(_c("2", f"  $ {command}\n"))
    if cwd:
        sys.stderr.write(_c("2", f"  (cwd: {cwd})\n"))
    sys.stderr.flush()

    if auto_approve:
        sys.stderr.write("  → auto-approved (--yes)\n")
        return "accept", None
    if not sys.stdin.isatty():
        sys.stderr.write("  → declined (no TTY; re-run interactively or pass --yes)\n")
        return "decline", None

    # Build the option list (mirrors the web card).
    APPROVE = "Approve"
    EDITS = "Approve & allow all edits"
    REMEMBER = f"Approve & don't ask again for {tool}" if tool else None
    BYPASS = "Approve & bypass all this session"
    REJECT = "Reject"
    options = [APPROVE]
    if allow_edits:
        options.append(EDITS)
    if REMEMBER:
        options.append(REMEMBER)
    options += [BYPASS, REJECT]

    choice = _select("Approve?", options, APPROVE)
    if choice in (None, REJECT):
        return "decline", None
    if choice == EDITS:
        return "accept", {"allow_all_edits": True}
    if choice == REMEMBER:
        if approvals is not None and tool:
            approvals.setdefault("remembered", set()).add(tool)
        return "accept", {"remember": True}
    if choice == BYPASS:
        if approvals is not None:
            approvals["bypass_all"] = True
        return "accept", {"bypass_all": True}
    return "accept", None


def render_output(text: str, *, label: bool) -> None:
    """Render the assistant's reply. In a TTY, render Markdown (bold/lists/code/
    headings) with rich — like the web. When piped/NO_COLOR, print plain text so
    scripts get clean output."""
    text = text.rstrip()
    if not text:
        return
    if label:
        print(_c("1;" + _PINK, "\n◆ loom"))
    if _COLOR and sys.stdout.isatty():
        try:
            from rich.console import Console
            from rich.markdown import Markdown

            Console().print(Markdown(text))
            return
        except Exception:  # noqa: BLE001 — fall back to plain on any rich issue
            pass
    print(text)


# The live panel currently on screen, so a Ctrl-C handler in the REPL can stop
# it (restore the cursor) before cancelling the turn. A list so we mutate, not
# rebind, from anywhere.
_ACTIVE_LIVE: list = []


class _AgentBoard:
    """Live per-agent activity for a turn. The lead agent plus every spawned
    worker (deep-research fans out worker *sessions*, surfaced on this stream as
    ``session.child_session.updated``) each get a named block showing their
    status and last few activity lines — so you see what each agent is doing,
    not just a spinner."""

    def __init__(self) -> None:
        self.agents: dict[str, dict] = {}
        self.order: list[str] = []

    def _ensure(self, key: str, name: str | None) -> dict:
        a = self.agents.get(key)
        if a is None:
            a = {"name": name or key, "status": "", "lines": collections.deque(maxlen=3),
                 "done": False, "reason": ""}
            self.agents[key] = a
            self.order.append(key)
        elif name:
            a["name"] = name
        return a

    def status(self, key: str, name: str | None, status: str, *, done: bool = False) -> None:
        a = self._ensure(key, name)
        a["status"] = status or a["status"]
        a["done"] = done

    def line(self, key: str, name: str | None, text: str) -> None:
        a = self._ensure(key, name)
        text = " ".join((text or "").split())
        if text and (not a["lines"] or a["lines"][-1] != text):
            a["lines"].append(text)

    def reason(self, key: str, name: str | None, text: str) -> None:
        self._ensure(key, name)["reason"] = " ".join((text or "").split())

    def render(self):
        from rich.console import Group
        from rich.text import Text

        blocks = []
        for key in self.order:
            a = self.agents[key]
            head = Text()
            head.append(("✓ " if a["done"] else "⟳ "), style="dim" if a["done"] else "bold #E01396")
            head.append(a["name"], style="bold #E01396")
            if a["status"]:
                head.append(f"  · {a['status']}", style="dim")
            blocks.append(head)
            if a["reason"] and not a["done"]:
                blocks.append(Text(f"    💭 {a['reason'][-100:]}", style="italic dim"))
            for ln in a["lines"]:
                blocks.append(Text(f"    {ln}", style="dim"))
        return Group(*blocks) if blocks else Text("  working…", style="dim")


def _tool_brief(name: str, arguments: str) -> str:
    """One-line human summary of a tool call for the activity board."""
    try:
        args = json.loads(arguments) if arguments else {}
    except (json.JSONDecodeError, TypeError):
        args = {}
    if isinstance(args, dict):
        for k in ("command", "query", "url", "path", "file_path", "pattern", "title", "prompt"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                v = " ".join(v.split())
                return f"{name} · {v[:70]}"
    return name


_URL_RE = re.compile(r"https?://[^\s)\]}<>\"'`]+")
# Workspace files the Loom skills write (deep-research, model-search, marimo, …).
_PATH_RE = re.compile(
    r"(?<![\w/])((?:research|logs|reports|notebooks|data|\.loom)/[\w./\-]+"
    r"|[\w./\-]*\.(?:ipynb|md|html|bib|csv|json)\b)"
)


def _collect_artifacts(text: str) -> tuple[list[str], list[str]]:
    """Pull (urls, workspace_paths) out of the assistant's reply. Loom skills
    report their outputs as text (paths / links), so scanning the reply is how a
    client surfaces them — see docs/LOOM_CLI.md."""
    urls, paths, seen = [], [], set()
    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;")
        if u not in seen:
            seen.add(u); urls.append(u)
    for m in _PATH_RE.finditer(text or ""):
        p = m.group(1).rstrip(".,;:)")
        if p not in seen and "/" in p and not p.startswith(("http", "www.")):
            seen.add(p); paths.append(p)
    return urls, paths


def _print_artifacts(urls: list[str], paths: list[str], files: list[tuple[str, str]]) -> None:
    """Show links (URLs), downloadable file artifacts, and workspace file paths
    produced during the turn, so the user can open/read them."""
    if not (urls or paths or files):
        return
    print(_c("1;" + _LOOM_PINK, "\n📎 artifacts"))
    for u in urls:
        print(f"  🔗 {u}")
    for name, url in files:
        print(f"  📄 {name}  →  {url}")
    for p in paths:
        print(_c("2", f"  🗂  {p}  (in your session workspace)"))


def _usage_footer(usage: dict) -> str | None:
    """Compact `· 12.3k/200k ctx · $0.04` footer from a session.usage payload."""
    if not usage:
        return None
    ctx, win, cost = usage.get("context_tokens"), usage.get("context_window"), usage.get("total_cost_usd")
    bits = []
    if ctx is not None:
        bits.append(f"{ctx/1000:.1f}k" + (f"/{win/1000:.0f}k" if win else "") + " ctx")
    if cost is not None:
        bits.append(f"${cost:.4f}".rstrip("0").rstrip("."))
    return " · ".join(bits) if bits else None


def run_turn(
    client: httpx.Client,
    server: str,
    session_id: str,
    text: str,
    *,
    label: bool = False,
    auto_approve: bool = False,
    sink: list | None = None,
    event: dict | None = None,
    approvals: dict | None = None,
    attachments: list[dict] | None = None,
    show_reasoning: bool = True,
) -> int:
    """Stream one turn: open stream, post the message (or ``event`` — e.g. a
    ``slash_command`` skill invocation — when given, or a message carrying
    ``attachments`` file/image parts), render a live per-agent activity panel
    (lead + spawned workers, todos, reasoning), print the assistant reply as
    Markdown, and surface tool-approval requests as terminal prompts. Ctrl-C
    interrupts the turn (not the REPL). ``sink`` also collects assistant text
    (plan output). ``approvals`` is session-scoped approval memory."""
    url = f"{server}/v1/sessions/{session_id}/stream"
    post_error: list[Exception] = []

    def _poster() -> None:
        time.sleep(0.25)  # let the stream establish first (no server-side replay)
        try:
            if event is not None:
                # Files attached just before a skill/slash invocation are delivered
                # as a user message first, so the skill run has them in context
                # (a bare ``slash_command`` event carries no content parts).
                if attachments:
                    _post_event(server, session_id,
                                {"type": "message",
                                 "data": {"role": "user", "content": list(attachments)}})
                _post_event(server, session_id, event)
            elif attachments:
                content = list(attachments)
                if text:
                    content.append({"type": "input_text", "text": text})
                _post_event(server, session_id,
                            {"type": "message", "data": {"role": "user", "content": content}})
            else:
                _post_message(server, session_id, text)
        except Exception as exc:  # noqa: BLE001
            post_error.append(exc)

    buf: list[str] = []
    last_status = None
    board = _AgentBoard()
    main_name = "loom"
    out_files: list[tuple[str, str]] = []  # (filename, download_url)
    writing = False
    todos: list[dict] = []
    usage: dict = {}
    reason_acc: list[str] = []

    def _render_all():
        from rich.console import Group
        from rich.text import Text
        blocks = []
        for t in todos:
            mark = {"completed": "✓", "in_progress": "▸"}.get(str(t.get("status")), "○")
            label_t = t.get("activeForm") if t.get("status") == "in_progress" else t.get("content")
            style = "dim" if t.get("status") == "completed" else ("#E01396" if t.get("status") == "in_progress" else "")
            blocks.append(Text(f"  {mark} {label_t or ''}", style=style))
        if todos:
            blocks.append(Text(""))
        blocks.append(board.render())
        return Group(*blocks)

    # A live panel only makes sense on an interactive terminal; piped/one-shot
    # runs keep clean, unadorned output.
    live = None
    if _COLOR and sys.stdout.isatty():
        try:
            from rich.console import Console
            from rich.live import Live

            live = Live(_render_all(), console=Console(), refresh_per_second=8,
                        transient=True, auto_refresh=False)
            live.start()
            _ACTIVE_LIVE[:] = [live]
        except Exception:  # noqa: BLE001
            live = None

    def _refresh() -> None:
        if live is not None:
            try:
                live.update(_render_all(), refresh=True)
            except Exception:  # noqa: BLE001
                pass

    def _finish(rc: int) -> int:
        _ACTIVE_LIVE.clear()
        if live is not None:
            try:
                live.stop()
            except Exception:  # noqa: BLE001
                pass
        answer = "".join(buf)
        render_output(answer, label=label)
        urls, paths = _collect_artifacts(answer)
        _print_artifacts(urls, paths, out_files)
        foot = _usage_footer(usage)
        if foot:
            print(_c("2", f"  · {foot}"))
        return rc

    with client.stream(
        "GET", url, headers={"Accept": "text/event-stream"}, timeout=_STREAM_TIMEOUT
    ) as resp:
        resp.raise_for_status()
        threading.Thread(target=_poster, daemon=True).start()
        for line in resp.iter_lines():
            if post_error:
                if live is not None:
                    live.stop()
                print(f"\n[error submitting turn] {post_error[0]}", file=sys.stderr)
                return 1
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            etype = str(ev.get("type") or "")
            short = etype.rsplit(".", 1)[-1].lower()  # normalize enum-leak variants
            if etype == "session.agent_changed" and ev.get("agent_name"):
                main_name = str(ev["agent_name"])
                continue
            if etype.startswith("session.status"):
                st = ev.get("status")
                if st and st != last_status:
                    last_status = st
                    if live is not None:
                        board.status(session_id, main_name, str(st))
                        _refresh()
                    elif st in ("launching", "running", "waiting"):
                        print(f"[{st}…]", file=sys.stderr, flush=True)
                continue
            if etype == "session.child_session.updated":
                child = ev.get("child") if isinstance(ev.get("child"), dict) else {}
                key = str(ev.get("child_session_id") or child.get("id") or "")
                if key:
                    cname = child.get("tool") or child.get("title") or child.get("agent_name") or "agent"
                    cstat = str(child.get("current_task_status") or ("running" if child.get("busy") else ""))
                    board.status(key, str(cname), cstat, done=cstat in ("completed", "failed"))
                    if child.get("last_message_preview"):
                        board.line(key, str(cname), str(child["last_message_preview"]))
                    _refresh()
                continue
            if etype == "session.usage":
                usage.update({k: ev.get(k) for k in ("context_tokens", "context_window", "total_cost_usd")})
                continue
            if etype == "session.todos":
                todos[:] = ev.get("todos") or []
                _refresh()
                continue
            if etype in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
                if show_reasoning:
                    reason_acc.append(str(ev.get("delta") or ""))
                    board.reason(session_id, main_name, "".join(reason_acc))
                    _refresh()
                continue
            if etype == "response.reasoning.started":
                continue
            if etype.startswith("response.compaction"):
                board.status(session_id, main_name,
                             "compacting context…" if etype.endswith("in_progress") else "running")
                _refresh()
                continue
            if etype == "response.output_item.done":
                item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
                who = str(item.get("model") or item.get("agent") or "") or session_id
                wname = main_name if who == session_id else None
                if item.get("type") == "function_call":
                    board.line(who, wname, "→ " + _tool_brief(str(item.get("name") or "tool"),
                                                              item.get("arguments") or ""))
                    _refresh()
                continue
            if etype == "response.output_file.done":
                fid = ev.get("file_id")
                if fid:
                    fname = str(ev.get("filename") or fid)
                    out_files.append((fname, f"{server}/v1/sessions/{session_id}/resources/files/{fid}/content"))
                continue
            if etype == "response.elicitation_request":
                eid = ev.get("elicitation_id")
                params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
                if eid:
                    # Worker prompts are mirrored here with target_session_id;
                    # resolve against the owning session, not always the lead.
                    target = str(params.get("target_session_id") or session_id)
                    if live is not None:
                        try:
                            live.stop()
                        except Exception:  # noqa: BLE001
                            pass
                    action, content = _decide_approval(
                        params, auto_approve=auto_approve, approvals=approvals)
                    try:
                        _resolve_elicitation(server, target, eid, action, content)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[approval failed to send: {exc}]", file=sys.stderr)
                    if live is not None:
                        try:
                            live.start()
                            _refresh()
                        except Exception:  # noqa: BLE001
                            pass
                continue
            if etype == "response.elicitation_resolved":
                # Answered elsewhere (e.g. the web app) — nothing to prompt.
                continue
            if etype == "response.output_text.delta":
                delta = ev.get("delta", "")
                buf.append(delta)
                if sink is not None:
                    sink.append(delta)
                if live is not None and not writing:
                    writing = True
                    board.reason(session_id, main_name, "")
                    board.status(session_id, main_name, "writing reply…")
                    _refresh()
            elif _terminal(short):
                if short in {"failed", "error"}:
                    if live is not None:
                        live.stop()
                    render_output("".join(buf), label=label)
                    err = (ev.get("response") or {}).get("error") or ev.get("error") or {}
                    print(f"\n[turn failed] {err}", file=sys.stderr)
                    return 1
                if short in {"incomplete", "cancelled"}:
                    if live is not None:
                        live.stop()
                    print(f"\n[turn {short}]", file=sys.stderr)
                    return _finish(0)
                break
    return _finish(0)


# The CLI's own REPL controls (client-side). Everything else that starts with
# `/` is treated as a skill invocation when it matches a session skill, else it
# falls through to a plaintext message — mirroring the web composer's routing.
# ``/plan`` is handled earlier in the loop (Claude plan mode, not a skill).
_BUILTIN_CMDS = {
    "/exit", "/quit", "/help", "/skills", "/settings", "/fast", "/compute",
    "/datasource", "/storage", "/connections", "/project", "/engine", "/model",
    "/effort", "/attach", "/image", "/title", "/archive", "/usage", "/todos",
    "/reasoning", "/sessions", "/share",
}

# (command, argument hint, one-line description) — the source for /help and docs.
_COMMAND_HELP = [
    ("/help", "", "list commands and what they do"),
    ("/skills", "", "list skills you can invoke"),
    ("/<skill>", "[args]", "run a skill, e.g. /deep-research <topic> (same as the web)"),
    ("/settings", "", "show engine · model · effort · fast · host · project · session"),
    ("/engine", "[name]", "list engines, or switch (resets model/effort to the engine default)"),
    ("/model", "[id]", "list models for the engine, or switch"),
    ("/effort", "[level]", "list/set reasoning effort (engines that support it)"),
    ("/fast", "[on|off]", "toggle Fast mode (Claude Opus only)"),
    ("/plan", "<task>", "plan a task in plan mode, then revert (Claude only)"),
    ("/project", "[name]", "show/move project ('' removes; groups sessions in the web UI)"),
    ("/attach", "<path>", "attach a file to your next message (uploaded to the session)"),
    ("/image", "<path>", "attach an image to your next message"),
    ("/datasource", "", "browse & attach a BigQuery table (needs a Google connection)"),
    ("/storage", "", "browse & attach a Cloud Storage bucket/object"),
    ("/connections", "", "show data connections (Google/GitHub/…)"),
    ("/sessions", "", "list your recent sessions (resume one with: loom --resume)"),
    ("/share", "", "publish this Pi session as HTML and print its public link"),
    ("/title", "<name>", "rename this session"),
    ("/archive", "", "archive this session (owner only)"),
    ("/usage", "", "show context tokens and session cost"),
    ("/todos", "", "show the agent's current todo list"),
    ("/reasoning", "[on|off]", "show/hide the agent's live reasoning (default on)"),
    ("/compute", "", "show compute tier (set at start via --compute)"),
    ("/exit", "", "quit (Ctrl-C during a turn cancels just that turn)"),
]


def _skill_slash(state: dict, client: httpx.Client, server: str, session_id: str, text: str):
    """If ``text`` is ``/<skill> [args]`` for a skill this session knows about,
    return its ``slash_command`` event; else ``None``. Refreshes the skill cache
    once on a miss, since runner-discovered skills can land after session start."""
    cmd = text.split()[0]
    name = cmd[1:]
    cache = state.get("_skills")
    if cache is None or name not in cache:
        cache = list_skills(client, server, session_id)
        state["_skills"] = cache
    if name in cache:
        return _skill_event(name, text[len(cmd):].strip())
    return None


def interactive(
    client: httpx.Client,
    server: str,
    session_id: str,
    *,
    agent_id: str,
    cfg: dict,
    project: str | None = None,
    auto_approve: bool = False,
) -> int:
    state = {"session_id": session_id, "project": project, **cfg}
    state["harness"] = state.get("harness") or DEFAULT_HARNESS
    # Session-scoped approval memory: a "bypass all" / "don't ask again" choice
    # is remembered and auto-applied to every later prompt — including spawned
    # worker agents, whose prompts are mirrored onto this stream.
    approvals: dict = {"bypass_all": False, "remembered": set()}
    state.setdefault("show_reasoning", True)
    state.setdefault("pending_files", [])
    # Data-source directive(s) to prepend to the next turn (mirrors the web).
    state["pending_directive"] = source_directives(state.get("bigquery"), state.get("gcs"))
    name = None
    try:
        me = client.get(f"{server}/v1/me", timeout=10.0).json()
        name = me.get("email") or me.get("user_id")
    except Exception:  # noqa: BLE001
        pass
    print_banner(server, session_id, state["harness"], project, model=state.get("model"), name=name)

    def _turn(text: str = "", *, event: dict | None = None, sink: list | None = None,
              attachments: list[dict] | None = None) -> None:
        """Run one turn; Ctrl-C cancels just this turn (not the REPL)."""
        try:
            run_turn(client, server, session_id, text, label=True, auto_approve=auto_approve,
                     approvals=approvals, event=event, sink=sink, attachments=attachments,
                     show_reasoning=state.get("show_reasoning", True))
        except KeyboardInterrupt:
            for lv in _ACTIVE_LIVE:
                try:
                    lv.stop()
                except Exception:  # noqa: BLE001
                    pass
            _ACTIVE_LIVE.clear()
            try:
                _interrupt(server, session_id)
            except Exception:  # noqa: BLE001
                pass
            print(_c("2", "\n  [turn cancelled]"))

    while True:
        try:
            text = input("\n" + _c("1;" + _PINK, "❯ ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        # `/plan <task>` runs that one task in plan mode, then reverts — no
        # explicit on/off (Claude only).
        if text == "/plan" or text.startswith("/plan "):
            task = text[len("/plan"):].strip()
            if not plan_supported(state["harness"]):
                print(f"  plan mode isn't available for {state['harness']} (Claude only)")
                continue
            if not task:
                print("  usage: /plan <task>  — plans that task (read-only), then asks to proceed")
                continue
            try:
                update_session(client, server, session_id, permission_mode="plan")
            except Exception as exc:  # noqa: BLE001
                print(_c("2", f"  couldn't enter plan mode: {exc}"))
                continue
            print(_c("1;" + _PINK, "  ◆ planning…"))
            full = state.get("pending_directive", "") + task
            state["pending_directive"] = ""
            # Consume any files queued with /attach so they reach the plan turn
            # rather than silently carrying over to a later message.
            atts = state.pop("pending_files", None) or None
            state["pending_files"] = []
            plan_text: list[str] = []
            _turn(full, sink=plan_text, attachments=atts)
            try:  # revert so the next message runs normally
                update_session(client, server, session_id, permission_mode="default")
            except Exception:  # noqa: BLE001
                pass
            saved = write_plan_file(task, "".join(plan_text))
            if saved:
                print(_c("2", f"  plan saved → {saved}"))
            continue
        if text.startswith("/"):
            cmd = text.split()[0]
            if cmd in _BUILTIN_CMDS:
                if not handle_command(client, server, agent_id, state, text):
                    return 0
                continue
            # A known skill → invoke it as the web does (slash_command event).
            ev = _skill_slash(state, client, server, session_id, text)
            if ev is not None:
                # Deliver any /attach-queued files with the skill run instead of
                # letting them leak into a later unrelated message.
                atts = state.pop("pending_files", None) or None
                state["pending_files"] = []
                _turn(event=ev, attachments=atts)
                continue
            # Not a loom command and not a known skill: the web sends this as a
            # plaintext message. Do the same, but say so (it may be a typo).
            print(_c("2", f"  '{cmd}' isn't a loom command or a known skill — "
                          f"sending as a message (see /help, /skills)"))
            turn_text = state.get("pending_directive", "") + text
            state["pending_directive"] = ""
            atts = state.pop("pending_files", None) or None
            state["pending_files"] = []
            _turn(turn_text, attachments=atts)
            continue
        turn_text = state.get("pending_directive", "") + text
        state["pending_directive"] = ""
        atts = state.pop("pending_files", None) or None
        state["pending_files"] = []
        _turn(turn_text, attachments=atts)


def ensure_local_server(server: str, timeout: float = 150.0) -> bool:
    """Make sure a local Loom server is up at ``server``; start ./run-stack.sh
    (via $LOOM_HOME) and wait if it isn't. Returns True when reachable."""
    def _up() -> bool:
        try:
            return httpx.get(f"{server}/v1/me", timeout=5.0).status_code < 500
        except Exception:  # noqa: BLE001
            return False

    if _up():
        return True
    home = os.environ.get("LOOM_HOME")
    script = os.path.join(home, "run-stack.sh") if home else None
    if not (script and os.path.exists(script)):
        print("  local server isn't running and run-stack.sh not found (set LOOM_HOME)", file=sys.stderr)
        return False
    print("  starting local Loom server (run-stack.sh)… this can take a minute.", file=sys.stderr)
    subprocess.Popen(["bash", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _up():
            print("  local server ready.", file=sys.stderr)
            return True
        time.sleep(3.0)
    print("  local server didn't come up in time — start it with ./run-stack.sh", file=sys.stderr)
    return False


def choose_compute(current: str) -> tuple[str, str | None, str | None]:
    """Interactive compute picker → (server, host_type, host_id).

    Thin-client mode: the user picks Loom managed compute (default)
    or a custom VM. No local-server option — loom-cli is a thin
    client like claude-cli; local dev uses ./run-stack.sh directly."""
    is_local = current.startswith("http://localhost") or current.startswith("http://127.0.0.1")
    if is_local:
        return current, None, None
    EPHEMERAL = "Loom ephemeral compute"
    VM = "Another VM…"
    pick = _select("Compute", [EPHEMERAL, VM], EPHEMERAL)
    if pick == VM:
        try:
            url = input(_c("1;" + _PINK, "  VM server URL: ")).strip().rstrip("/")
        except (EOFError, KeyboardInterrupt):
            url = ""
        return (url or current), None, None
    srv = current if current.startswith("https") else (os.environ.get("LOOM_REMOTE") or "https://loom.mbd.xyz")
    print(_c("2", f"  ephemeral compute on {srv}"), file=sys.stderr)
    return srv, "managed", None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="loom", add_help=True)
    ap.add_argument("--server", default=os.environ.get("LOOM_SERVER", "https://loom.mbd.xyz"),
                    help="Loom server base URL (default: https://loom.mbd.xyz or $LOOM_SERVER)")
    ap.add_argument("--harness", help="brain harness (claude-sdk | pi | codex | …)")
    ap.add_argument("--model", help="model override")
    ap.add_argument("--effort", choices=ALL_EFFORTS, help="reasoning effort (engine-dependent vocab)")
    ap.add_argument(
        "--no-wizard",
        action="store_true",
        help="skip the start-of-session settings prompts (use defaults/flags)",
    )
    ap.add_argument("-p", "--prompt", help="one-shot: send this and print the reply")
    ap.add_argument("--session", help="reuse an existing session id (conv_…)")
    ap.add_argument(
        "--create-only",
        action="store_true",
        help="create a session, wait until its runner is live, print its id, and exit "
        "(used by the wrapper to hand off to the branded interactive REPL)",
    )
    ap.add_argument(
        "--workspace",
        help="local dir for an external host (default: cwd), or a git repo spec "
        "(<url>[#branch]) for a managed sandbox",
    )
    ap.add_argument("--host", help="run on a specific registered host id (your machine/VM)")
    ap.add_argument("--managed", action="store_true", help="run on Loom ephemeral compute (managed sandbox)")
    ap.add_argument(
        "--project",
        help="file this session under a project (default: repo/dir name, else Scratch)",
    )
    ap.add_argument("--fast", action="store_true", help="Fast mode")
    ap.add_argument("--plan", action="store_true", help="Plan mode")
    ap.add_argument("--compute", choices=COMPUTE_TIERS, help="managed compute tier (default: cpu-8g)")
    ap.add_argument("--exec-timeout", type=int, dest="exec_timeout", help="managed exec timeout (s)")
    ap.add_argument("--lifetime", type=int, dest="lifetime", help="managed sandbox lifetime (s)")
    ap.add_argument("--idle-timeout", type=int, dest="idle_timeout", help="managed idle timeout (s)")
    ap.add_argument("--aide-code-model", dest="aide_code_model", help="AIDE code model (managed)")
    ap.add_argument("--aide-feedback-model", dest="aide_feedback_model", help="AIDE feedback model (managed)")
    ap.add_argument("--aide-report-model", dest="aide_report_model", help="AIDE report model (managed)")
    ap.add_argument("--bigquery", help="attach a BigQuery data source (project.dataset.table)")
    ap.add_argument("--gcs", help="attach a Cloud Storage data source (bucket[/prefix])")
    ap.add_argument("--attach", action="append", metavar="PATH",
                    help="attach a file to the first message (repeatable)")
    ap.add_argument("--image", action="append", metavar="PATH",
                    help="attach an image to the first message (repeatable)")
    ap.add_argument("--list-sessions", dest="list_sessions", action="store_true",
                    help="list your recent sessions and exit")
    ap.add_argument("--resume", action="store_true",
                    help="pick a recent session to resume (interactive)")
    ap.add_argument("--reasoning", dest="reasoning", action=argparse.BooleanOptionalAction,
                    default=None, help="show the agent's live reasoning (default: on / your saved default)")
    ap.add_argument(
        "--yes",
        "--approve-all",
        dest="yes",
        action="store_true",
        help="auto-approve tool-approval requests (for non-interactive/scripted runs)",
    )
    ap.add_argument("command", nargs="?", default=None,
                    help="subcommand: login, logout, whoami, sessions, resume")
    args = ap.parse_args(argv)
    server = args.server.rstrip("/")

    # Auth subcommands are normally handled by the front door (``__main__``);
    # handle them here too so ``client.main`` stays self-contained if run directly.
    if args.command in ("login", "logout", "whoami"):
        from . import auth
        if args.command == "login":
            return auth.login(server)
        if args.command == "logout":
            return auth.logout(server)
        return auth.whoami(server)
    if args.command == "sessions":
        args.list_sessions = True
    elif args.command == "resume":
        args.resume = True

    cfg = {
        "harness": args.harness, "model": args.model, "effort": args.effort,
        # None (not False) when --fast is absent, so a saved default can fill it.
        "fast": True if args.fast else None, "plan": args.plan, "compute": args.compute,
        "exec_timeout": args.exec_timeout, "lifetime": args.lifetime,
        "idle_timeout": args.idle_timeout, "aide_code": args.aide_code_model,
        "aide_feedback": args.aide_feedback_model, "aide_report": args.aide_report_model,
        "bigquery": args.bigquery, "gcs": args.gcs,
        "host_type": ("managed" if args.managed else ("external" if args.host else None)),
        "host_id": args.host,
    }
    project = args.project
    is_interactive = not args.prompt and not args.create_only and not args.session

    # Compute chooser (interactive, no explicit compute/host flag): pick the
    # backend — Loom ephemeral, this machine (local server, auto-started), or a
    # VM — before connecting, since that determines the server. Skipped when
    # just listing or resuming (those reuse an existing session's compute).
    if (is_interactive and not args.no_wizard and not args.managed and not args.host
            and not args.list_sessions and not args.resume and sys.stdin.isatty()):
        server, cfg["host_type"], cfg["host_id"] = choose_compute(server)

    # The target server is now final (Compute can point at prod / localhost / a
    # custom VM). Fill unset fields from THIS server's saved defaults — defaults
    # are kept per-server — so we don't re-ask them. ``first_run`` = this server
    # has no saved bucket yet → run the full wizard once (seeded from any prior
    # prefs), then persist under this server.
    _seed = server_defaults(server)
    apply_defaults(cfg, _seed)
    cfg["fast"] = bool(cfg.get("fast"))
    first_run = not defaults_configured(server)

    with httpx.Client(headers=_auth_headers(server), timeout=30.0) as client:
        try:
            # Browse recent sessions (the CLI's sidebar) and exit.
            if args.list_sessions:
                rows = list_sessions(client, server)
                print(_c("1;" + _LOOM_PINK, "your recent sessions"))
                for s in rows:
                    print("  " + _session_row(s))
                return 0
            # Resume: pick a recent session, then fall through as if --session.
            if args.resume and not args.session:
                rows = list_sessions(client, server)
                if not rows:
                    print("no sessions to resume", file=sys.stderr)
                    return 1
                labels = {_session_row(s): s.get("id") for s in rows}
                pick = _select("Resume which session?", list(labels), next(iter(labels)))
                if not pick:
                    return 0
                args.session = labels[pick]

            agent_id = resolve_loom_agent_id(client, server)
            if args.session:
                session_id = args.session
            else:
                # Interactive start: prompt only for what the engine supports
                # (engine · model · effort? · compute?), unless --no-wizard.
                if is_interactive and not args.no_wizard:
                    cfg = run_wizard(client, server, agent_id, cfg, ask_all=first_run)
                    # Remember these answers as this server's defaults (compute/
                    # tier excluded — always asked). Mid-session /commands too.
                    save_defaults(server, cfg)
                # Drop engine-unsupported knobs so we never send an invalid combo.
                h = cfg["harness"] or DEFAULT_HARNESS
                if cfg.get("effort") and cfg["effort"] not in (effort_levels(h) or []):
                    print(f"note: {h} ignores reasoning effort — dropping", file=sys.stderr)
                    cfg["effort"] = None
                if cfg.get("fast") and not fast_supported(h, cfg.get("model")):
                    print(f"note: fast mode isn't supported by {h} — dropping", file=sys.stderr)
                    cfg["fast"] = False
                if cfg.get("plan") and not plan_supported(h):
                    print(f"note: plan mode isn't supported by {h} — dropping", file=sys.stderr)
                    cfg["plan"] = False
                if cfg.get("plan") and is_interactive:
                    # Interactive uses `/plan <task>` per turn, not a persistent mode.
                    print("note: use `/plan <task>` in interactive mode — ignoring --plan", file=sys.stderr)
                    cfg["plan"] = False
                execution = resolve_execution(
                    client, server, host_type=cfg.get("host_type"), host_id=cfg.get("host_id"),
                    workspace_arg=args.workspace,
                    compute=cfg["compute"], exec_timeout_s=cfg["exec_timeout"],
                    sandbox_lifetime_s=cfg["lifetime"], idle_timeout_s=cfg["idle_timeout"],
                    aide_models={
                        "aide_code_model": cfg["aide_code"],
                        "aide_feedback_model": cfg["aide_feedback"],
                        "aide_report_model": cfg["aide_report"],
                    },
                )
                session_id = create_session(
                    client,
                    server,
                    agent_id,
                    harness=cfg["harness"],
                    model=cfg["model"],
                    effort=cfg["effort"],
                    fast=cfg["fast"],
                    plan=cfg["plan"],
                    execution=execution,
                )
                # File it under a project so it groups like the web UI: an
                # explicit --project, else the repo/dir name, else "Scratch".
                project = args.project or derive_project(execution.get("workspace")) or DEFAULT_PROJECT
                try:
                    set_project(client, server, session_id, project)
                except Exception:  # noqa: BLE001 — non-fatal, mirrors the web
                    project = args.project
        except httpx.HTTPStatusError as exc:
            print(
                f"server error {exc.response.status_code}: {exc.response.text[:200]}",
                file=sys.stderr,
            )
            return 1

        if args.create_only:
            if not wait_until_live(client, server, session_id):
                # Don't emit a "ready" session id or exit 0 — automation would
                # treat an unavailable runner as ready and proceed on it.
                print(f"session {session_id} did not become live in time", file=sys.stderr)
                return 1
            print(session_id)
            return 0

        # Start local file/shell tools with a WebSocket tunnel to the server.
        # The remote agent calls local_read/local_write/local_shell through
        # the tunnel — the server proxies the call to this CLI process.
        _local_tunnel_thread = None
        _is_remote = not server.startswith("http://localhost") and not server.startswith("http://127.0.0.1")
        if _is_remote or os.environ.get("LOOM_LOCAL_TOOLS"):
            try:
                from . import local_tools as _lt
                _lt._project_dir = Path(args.workspace or ".").resolve()

                def _run_tunnel():
                    import websockets.sync.client as wsc
                    ws_url = server.replace("https://", "wss://").replace("http://", "ws://")
                    ws_url = f"{ws_url}/v1/sessions/{session_id}/local-tools"
                    headers = _auth_headers(server)
                    conn_kwargs = {"additional_headers": headers, "close_timeout": 5}
                    # For wss, verify against certifi's CA bundle (as httpx does).
                    # A frozen (PyInstaller) build has no system CA path, so the
                    # stdlib default context fails with CERTIFICATE_VERIFY_FAILED.
                    if ws_url.startswith("wss://"):
                        import ssl as _ssl
                        try:
                            import certifi
                            conn_kwargs["ssl"] = _ssl.create_default_context(cafile=certifi.where())
                        except Exception:
                            conn_kwargs["ssl"] = _ssl.create_default_context()
                    try:
                        with wsc.connect(ws_url, **conn_kwargs) as ws:
                            print(f"local tools: tunnel connected ({args.workspace or '.'})", file=sys.stderr)
                            while True:
                                raw = ws.recv()
                                frame = json.loads(raw)
                                if frame.get("type") == "tool_call":
                                    try:
                                        output = _lt._handle_tool_call(frame["tool"], frame.get("arguments", {}))
                                        ws.send(json.dumps({"type": "tool_result", "id": frame["id"], "output": output}))
                                    except Exception as e:
                                        ws.send(json.dumps({"type": "tool_error", "id": frame["id"], "error": str(e)}))
                                elif frame.get("type") == "ping":
                                    ws.send(json.dumps({"type": "pong"}))
                    except Exception as e:
                        print(f"local tools: tunnel closed ({e})", file=sys.stderr)

                import threading
                _local_tunnel_thread = threading.Thread(target=_run_tunnel, daemon=True)
                _local_tunnel_thread.start()
            except Exception as exc:
                print(f"local tools: not started ({exc})", file=sys.stderr)

        # Upload any files/images to attach to the first message.
        init_atts: list[dict] = []
        for p in (args.attach or []) + (args.image or []):
            part = upload_attachment(client, server, session_id, p)
            if part:
                init_atts.append(part)
        cfg["show_reasoning"] = (
            args.reasoning if args.reasoning is not None
            else _seed.get("show_reasoning", True)
        )

        if args.prompt:
            # `-p "/deep-research <topic>"` runs that skill (web parity); other
            # slashes and plain text go through as a normal message.
            approvals: dict = {"bypass_all": False, "remembered": set()}
            if args.prompt.strip() == "/share":
                try:
                    response = client.post(
                        f"{server}/v1/sessions/{session_id}/pi-share",
                        timeout=75.0,
                    )
                    response.raise_for_status()
                    print(response.json()["url"])
                    return 0
                except httpx.HTTPStatusError as exc:
                    print(
                        f"share failed: {exc.response.status_code} {exc.response.text[:200]}",
                        file=sys.stderr,
                    )
                    return 1
            skill_ev = None
            if args.prompt.startswith("/"):
                cmd = args.prompt.split()[0]
                skills = list_skills(client, server, session_id)
                if cmd[1:] in skills:
                    skill_ev = _skill_event(cmd[1:], args.prompt[len(cmd):].strip())
            if skill_ev is not None:
                return run_turn(client, server, session_id, "", auto_approve=args.yes,
                                event=skill_ev, approvals=approvals)
            text = source_directives(cfg["bigquery"], cfg["gcs"]) + args.prompt
            plan_sink: list[str] | None = [] if cfg.get("plan") else None
            rc = run_turn(client, server, session_id, text, auto_approve=args.yes,
                          sink=plan_sink, approvals=approvals,
                          attachments=init_atts or None, show_reasoning=args.reasoning)
            if plan_sink is not None:
                saved = write_plan_file(args.prompt, "".join(plan_sink))
                if saved:
                    print(f"plan saved → {saved}", file=sys.stderr)
            return rc
        cfg["pending_files"] = init_atts
        return interactive(
            client,
            server,
            session_id,
            agent_id=agent_id,
            cfg=cfg,
            project=project,
            auto_approve=args.yes,
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
