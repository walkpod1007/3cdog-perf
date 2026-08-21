"""Entrypoint for the ``3cdog-perf`` console script.

The packaged variant replaces the old ``android_perf.py`` stub. Two subcommands
are exposed:

* ``serve`` (default) — start the local UI server and open the browser.
* ``--version`` — print the package version and exit (used by Homebrew's
  ``test do`` block and by humans sanity-checking the install).

The server module is the same stdlib-only HTTP front-end that ships inside
the ``perf_server`` package; the only thing this file adds is a friendlier
CLI surface and an automatic browser-open on macOS.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from typing import List, Optional
from pathlib import Path


__version__ = "0.2.0"


def _open_browser_when_ready(url: str, server_ref: list) -> None:
    """Poll the server until it accepts a socket, then open the URL once.

    ``server_ref`` is a one-element list used as a mutable box so the polling
    thread can cooperatively shut down when the main thread exits. We open
    the browser in a daemon thread so a headless install (CI / SSH) does not
    hang waiting for a desktop session.
    """
    import socket
    import time

    host, port = url.rsplit(":", 1)
    host = host.split("//", 1)[-1] or "127.0.0.1"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not server_ref or server_ref[0] is None:
            return
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        return
    try:
        webbrowser.open(url)
    except Exception:
        # Never fail the server on a browser-open error; that would punish
        # headless / SSH installs.
        pass


def _bootstrap_server(args: argparse.Namespace) -> int:
    """Import :mod:`perf_server.server` lazily and start it.

    Doing the import inside a function keeps ``3cdog-perf --version`` snappy
    (no module-resolution side effects) and lets the package's import path
    rules stay as simple as ``python -m perf_server``.
    """
    from perf_server import server

    sessions_dir = args.sessions_dir
    if sessions_dir is None:
        # Default lives under the user's home so successive package upgrades
        # don't wipe recorded sessions. Users can still override via
        # ``--sessions-dir`` for ephemeral benches.
        sessions_dir = Path.home() / ".3cdog-perf" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    orphans = server.reap_orphan_collectors()
    if orphans:
        print(
            "reaped {} orphan collector process(es)".format(orphans),
            file=sys.stderr,
        )
    httpd = server.make_server(args.host, args.port, sessions_dir)
    host, port = httpd.server_address[:2]
    url = "http://{}:{}".format(host, port)
    print("3cdog Perf UI: {}".format(url))
    print("Sessions: {}".format(sessions_dir))
    print("Tip: open the URL above if a browser tab did not open automatically.")

    server_box: List[Optional[object]] = [httpd]
    if args.open_browser and sys.platform == "darwin" and os.environ.get("DISPLAY") is None:
        # ``DISPLAY`` is unset on macOS terminals; we trust that as a "desktop
        # session is available" signal. SSH / CI environments without a
        # window server will not satisfy this branch.
        thread = threading.Thread(
            target=_open_browser_when_ready,
            args=(url, server_box),
            daemon=True,
        )
        thread.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server_box[0] = None
        httpd.server_close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="3cdog-perf",
        description="Local FPS / CPU / GPU / power profiler for iPhone and Android.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the package version and exit",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="UI server bind host (default: 127.0.0.1; never expose publicly)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="UI server bind port (default: 8765)",
    )
    parser.add_argument(
        "--sessions-dir",
        type=str,
        default=None,
        help="where to keep session .jsonl files (default: <package>/sessions)",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_false",
        dest="open_browser",
        default=True,
        help="do not try to open the UI in a browser tab on startup",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print("3cdog-perf {}".format(__version__))
        return 0
    return _bootstrap_server(args)


if __name__ == "__main__":
    raise SystemExit(main())
