"""The ``loom`` command-line front door.

Sign in once, then run the Loom agent from your terminal against a Loom server —
the same account, keys, and engines as the web app. Mirrors how ``claude`` works:
``loom login``, then ``loom``.

Usage:
  loom login [url] [--sso] [--connector google|github]
                              Sign in to the Loom server (stores a token)
  loom logout [url]           Forget the stored token for this server
  loom whoami [url]           Show who you're signed in as
  loom                        Interactive session on the Loom server
  loom -p "question"          One-shot: ask and get the answer
  loom sessions               List your recent sessions
  loom resume                 Pick a recent session to resume
  loom --server <url> …       Target a specific server for this invocation
  loom --version              Print the CLI version

Server precedence: ``--server <url>`` (or a URL after login/logout/whoami) >
LOOM_SERVER > the last server you logged in to > https://loom.mbd.xyz.
Point it at a local server with ``loom --server http://localhost:6767 …``.
"""

from __future__ import annotations

import sys

from . import __version__
from . import auth, client, config


def _usage() -> None:
    print(__doc__.strip())


def _extract_server(argv: list[str]) -> tuple[str | None, list[str]]:
    """Pull ``--server <url>`` / ``--server=<url>`` from anywhere in argv."""
    server: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--server" and i + 1 < len(argv):
            server = argv[i + 1]
            i += 2
            continue
        if a.startswith("--server="):
            server = a[len("--server=") :]
            i += 1
            continue
        rest.append(a)
        i += 1
    return server, rest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0
    if argv and argv[0] in ("-V", "--version", "version"):
        print(f"loom {__version__}")
        return 0

    server_override, argv = _extract_server(argv)

    cmd = argv[0] if argv else None

    # ── explicit auth subcommands (accept an optional positional URL) ──
    if cmd in ("login", "logout", "whoami"):
        rest = argv[1:]
        # split an optional server URL positional from flags
        url: str | None = None
        flags: list[str] = []
        for a in rest:
            if a.startswith("-"):
                flags.append(a)
            elif url is None:
                url = a
            else:
                flags.append(a)
        server = config.resolve_server(url or server_override)
        if cmd == "login":
            sso = "--sso" in flags
            connector = None
            for f in flags:
                if f.startswith("--connector="):
                    connector = f[len("--connector=") :]
                elif f == "--connector" and flags.index(f) + 1 < len(flags):
                    connector = flags[flags.index(f) + 1]
            rc = auth.login(server, sso=sso, connector=connector)
            if rc == 0:
                config.remember_default_server(server)
            return rc
        if cmd == "logout":
            return auth.logout(server)
        return auth.whoami(server)

    # ── subcommands that map to client flags ──
    if cmd == "sessions":
        argv = ["--list-sessions", *argv[1:]]
    elif cmd == "resume":
        argv = ["--resume", *argv[1:]]

    # ── default: a session on the Loom server ──
    server = config.resolve_server(server_override)

    # Ensure we're signed in (no-op on header/single-user servers).
    if auth.check_token(server) != 0:
        print(f"→ Signing in to Loom ({server})…")
        rc = auth.login(server)
        if rc != 0:
            return rc
        config.remember_default_server(server)

    return client.main(["--server", server, *argv])


if __name__ == "__main__":
    raise SystemExit(main())
