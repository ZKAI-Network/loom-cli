"""Auth for the ``loom`` CLI: token storage + the sign-in flow.

Self-contained (no external engine dependency). Persists a per-server session
token in ``~/.loom/auth.json`` keyed by server URL, and signs in against a Loom
server by auto-detecting its auth mode:

- **header / single-user** — the proxy injects identity; no login needed.
- **accounts** — username + password → ``POST /auth/login``.
- **SSO / OIDC** — browser ticket flow: request a ticket, open the browser,
  poll until the session token is minted.
"""

from __future__ import annotations

import json
import os
import stat
import time
import webbrowser
from getpass import getpass
from pathlib import Path

import httpx

_TOKEN_FILE_NAME = "auth.json"
_CLI_LOGIN_TIMEOUT_SECONDS = 300  # 5 minutes


# ─────────────────────────── token store ────────────────────────────


def _state_dir() -> Path:
    """Return the Loom CLI state directory (``~/.loom``), honoring ``LOOM_HOME_DIR``."""
    override = os.environ.get("LOOM_STATE_DIR")
    base = Path(override) if override else Path.home() / ".loom"
    return base


def _token_file_path() -> Path:
    return _state_dir() / _TOKEN_FILE_NAME


def _normalize_server_url(server_url: str) -> str:
    """Strip trailing slashes so ``…:6767`` and ``…:6767/`` key the same entry."""
    return server_url.rstrip("/")


def _read_all() -> dict[str, dict[str, str | float]]:
    """Load the whole token map from ``~/.loom/auth.json``."""
    path = _token_file_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _store_entry(server_url: str, entry: dict[str, str | float]) -> None:
    """Create/update a server's record in ``~/.loom/auth.json`` (mode 0600)."""
    path = _token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_all()
    data[_normalize_server_url(server_url)] = entry
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def store_token(server_url: str, token: str, user_id: str, expires_at: float) -> None:
    """Persist a session token for a server."""
    _store_entry(
        server_url,
        {"token": token, "user_id": user_id, "expires_at": expires_at},
    )


def _load_entry(server_url: str) -> dict[str, str | float] | None:
    entry = _read_all().get(_normalize_server_url(server_url))
    return entry if isinstance(entry, dict) else None


def load_token(server_url: str) -> str | None:
    """Return a stored, unexpired session token for a server, else ``None``."""
    entry = _load_entry(server_url)
    if entry is None:
        return None
    expires_at = entry.get("expires_at", 0)
    if isinstance(expires_at, (int, float)) and expires_at < time.time():
        return None
    token = entry.get("token")
    return token if isinstance(token, str) else None


def clear_token(server_url: str) -> None:
    """Remove a stored token for a server (no-op if absent)."""
    path = _token_file_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    key = _normalize_server_url(server_url)
    if key in data:
        del data[key]
        path.write_text(json.dumps(data, indent=2))


# ─────────────────────────── helpers ────────────────────────────


class LoginError(Exception):
    """A sign-in failure with a user-facing message."""


def _headers(server: str) -> dict[str, str]:
    tok = load_token(server)
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# ─────────────────────────── sign-in flows ────────────────────────────


def _accounts_login(server: str) -> None:
    """Accounts mode: prompt for username + password, POST /auth/login."""
    print(f"Signing in to {server} (accounts auth).")
    username = input("Username [admin]: ").strip() or "admin"
    password = getpass("Password: ")
    try:
        resp = httpx.post(
            f"{server}/auth/login",
            json={"username": username, "password": password},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise LoginError(f"Could not reach {server}/auth/login: {exc}") from exc

    if resp.status_code == 401:
        raise LoginError("Invalid username or password.")
    if resp.status_code >= 500:
        raise LoginError("Server error during login. Try again in a moment.")
    if not resp.is_success:
        raise LoginError(f"Login failed ({resp.status_code}): {resp.text[:200]}")

    body = resp.json()
    token = body["token"]
    user_id = body["user"]["id"]
    expires_in = body.get("expires_in", 8 * 3600)
    store_token(server, token, user_id, time.time() + expires_in)
    print(f"Logged in as {user_id}.")


def _ticket_login(server: str, base_path: str, connector: str | None = None) -> None:
    """Browser ticket flow (SSO/OIDC). ``base_path`` is ``/auth`` or ``/sso``."""
    start = f"{server}{base_path}/cli-login"
    if connector:
        start += f"?connector={connector}"
    try:
        resp = httpx.post(start, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise LoginError(
            f"Could not reach {start}: {exc}\nIs SSO enabled on this server?"
        ) from exc

    data = resp.json()
    ticket = data["ticket"]
    login_url = f"{server}{data['login_url']}"

    print(f"Opening browser for login: {login_url}")
    print("If it didn't open, paste that URL into any browser.")
    print("Waiting for authentication…")
    try:
        webbrowser.open(login_url)
    except Exception:  # noqa: BLE001 - headless boxes have no browser; the URL is printed above
        pass

    poll_url = f"{server}{base_path}/cli-poll?ticket={ticket}"
    deadline = time.time() + _CLI_LOGIN_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(2)
        try:
            poll = httpx.get(poll_url, timeout=10.0)
        except httpx.HTTPError:
            continue
        if poll.status_code == 202:
            continue  # still pending
        if poll.status_code == 200:
            result = poll.json()
            expires_in = result.get("expires_in", 8 * 3600)
            store_token(
                server,
                token=result["token"],
                user_id=result["user_id"],
                expires_at=time.time() + expires_in,
            )
            print(f"Logged in as {result['user_id']}")
            return
        raise LoginError("Login ticket expired or was rejected. Please try again.")
    raise LoginError(
        f"Login timed out — the browser flow was not completed within "
        f"{_CLI_LOGIN_TIMEOUT_SECONDS} seconds."
    )


def login(server: str, *, sso: bool = False, connector: str | None = None) -> int:
    """Sign in to a Loom server, auto-detecting its auth mode.

    :param server: normalized server base URL (no trailing slash).
    :param sso: force the ``/sso`` hybrid browser flow (accounts server with
        SSO buttons), skipping the username/password prompt.
    :param connector: optional SSO connector id (``google``/``github``) to skip
        the provider chooser and go straight to that provider.
    :returns: process exit code.
    """
    server = server.rstrip("/")

    # Explicit SSO request → the hybrid /sso browser flow.
    if sso or connector:
        try:
            _ticket_login(server, "/sso", connector=connector)
            return 0
        except LoginError as exc:
            print(str(exc))
            return 1

    # Probe the server's auth mode via /v1/me.
    try:
        probe = httpx.get(f"{server}/v1/me", timeout=10.0)
    except httpx.HTTPError as exc:
        print(f"Could not reach {server}/v1/me: {exc}\nIs the server running?")
        return 1

    if probe.status_code == 200:
        print(
            f"{server} is in header-auth mode — no login needed. "
            "The proxy in front of it injects your identity on every request."
        )
        return 0

    detected_login_url: str | None = None
    if probe.status_code == 401:
        try:
            detected_login_url = probe.json().get("login_url")
        except ValueError:
            detected_login_url = None

    try:
        if detected_login_url == "/login":
            _accounts_login(server)
        else:
            # OIDC mode (or unknown — the ticket endpoint gives a clear error).
            _ticket_login(server, "/auth")
    except LoginError as exc:
        print(str(exc))
        return 1
    return 0


# ─────────────────────── auth subcommands (whoami/logout/check) ───────────────


def check_token(server: str) -> int:
    """Exit 0 if signed in (valid token, or a server that needs none)."""
    server = server.rstrip("/")
    if load_token(server):
        return 0
    try:
        return 0 if httpx.get(f"{server}/v1/me", timeout=10.0).status_code == 200 else 1
    except Exception:  # noqa: BLE001
        return 1


def whoami(server: str) -> int:
    server = server.rstrip("/")
    try:
        r = httpx.get(f"{server}/v1/me", headers=_headers(server), timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        print(f"could not reach {server}: {exc}")
        return 1
    if r.status_code != 200:
        print("not signed in — run `loom login`")
        return 1
    me = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    print(me.get("email") or me.get("user_id") or me.get("id") or "signed in")
    return 0


def logout(server: str) -> int:
    server = server.rstrip("/")
    clear_token(server)
    print(f"Signed out of {server}")
    return 0
