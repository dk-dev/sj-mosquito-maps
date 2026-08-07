"""
Desktop launcher.

Wraps serve.py + index.html in a single native window (via pywebview), so the
map opens like an application instead of a terminal command plus a remembered
URL. Works on Windows, macOS and Linux, and falls back to the default browser
if pywebview is not installed or cannot start.

  python app.py              open the map
  python app.py --refresh    re-scrape the district's page first, then open

Unlike the sibling weather-map project, this one COMMITS data/, so a fresh
clone already has the full archive and the map opens instantly with no fetch.

FROZEN-BUILD NOTES
------------------
Nothing here resolves data through ``__file__``. In a PyInstaller onefile build
``__file__`` points into a temp directory that is deleted when the app exits,
so an archive written there would vanish; ``sjmvcd.paths`` puts the writable
archive under the user's app-data directory instead. Two consequences show up
in this file:

  * ``paths.seed_data_dir()`` runs before the server starts, so the very first
    launch copies the archive that shipped inside the exe out to that writable
    location. It never overwrites, so later launches leave a user's updated
    archive alone.
  * the fetcher is called in-process. ``sys.executable`` in a frozen app is the
    app itself, so spawning it would relaunch the GUI rather than run a scrape,
    and ``fetch_data.py`` is not on disk to be spawned in the first place.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from sjmvcd import paths

# reuse the same handler + two-root mapping that powers `python serve.py`
import serve
# imported explicitly (rather than reached through `serve.fetch_data`) so that
# PyInstaller's static analysis sees it from this entry point too
import fetch_data

WINDOW_TITLE = "San Joaquin County spray timelapse"


def find_free_port() -> int:
    """Bind to port 0, let the OS pick, return the picked port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int) -> ThreadingHTTPServer:
    """Start serve.py's handler in a daemon thread. Returns the server."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), serve.make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd


def run_fetch(reason: str) -> None:
    """
    Run the fetcher in-process, streaming its output.

    In-process, not a subprocess: see the frozen-build note in the module
    docstring. Failures are reported and swallowed -- the archive on disk is
    left untouched by a failed run, and opening yesterday's map beats showing
    the user a stack trace.
    """
    print(f"[app] {reason}")
    try:
        returncode = fetch_data.main([])
    except Exception as exc:
        print(f"[app] Fetch failed ({exc!r}).")
        returncode = 1
    if returncode != 0:
        print("[app] Fetch failed. Opening the archive that is already on disk.")


def ensure_data(refresh: bool) -> None:
    """
    Make sure a readable archive exists at paths.data_dir() before serving.

    Seeding first is what makes a frozen first run work: the exe carries a
    read-only copy of the archive, and this lifts it into the writable
    location. It only ever copies files that are MISSING, so a user who has
    pressed "update maps" never gets rolled back to the build-time snapshot.
    In a dev checkout the source and destination are the same directory and
    this is a no-op.
    """
    copied, data_root = paths.seed_data_dir()
    if copied:
        print(f"[app] Installed {copied} archive file(s) into {data_root}")

    if refresh:
        run_fetch("Refreshing from the district's spray-alerts page...")
        return
    if not (data_root / "operations.json").exists():
        run_fetch("No archive found. Building it (one-time)...")


def open_window(url: str) -> bool:
    """Try to open a native window via pywebview. Return True if it worked."""
    try:
        import webview  # pywebview
    except ImportError:
        print("[app] pywebview not installed - opening in your browser instead.")
        print("[app] For a native window: pip install -r requirements-desktop.txt")
        return False
    try:
        webview.create_window(
            WINDOW_TITLE, url,
            width=1500, height=950,
            min_size=(1000, 640),
        )
        webview.start()   # blocks until the window is closed
        return True
    except Exception as exc:
        print(f"[app] pywebview could not start ({exc}); falling back to browser.")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true",
                        help="re-scrape the district's page before opening")
    parser.add_argument("--browser", action="store_true",
                        help="skip the native window and use the default browser")
    args = parser.parse_args(argv)

    print(f"[app] {paths.describe()}")
    ensure_data(args.refresh)

    port = find_free_port()
    httpd = start_server(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"[app] Serving {serve.BUNDLE_ROOT} on {url}")

    if args.browser or not open_window(url):
        webbrowser.open(url)
        print("[app] Map opened in your browser. Press Ctrl-C here to stop the server.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\n[app] Interrupted.")

    httpd.shutdown()
    print("[app] Server stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
