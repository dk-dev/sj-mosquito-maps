"""
Desktop launcher.

Wraps serve.py + index.html in a single native window (via pywebview), so the
map opens like an application instead of a terminal command plus a remembered
URL. Works on Windows, macOS and Linux, and falls back to the default browser
if pywebview is not installed or cannot start.

  python app.py              open the map
  python app.py --refresh    re-scrape the district's page first, then open

Unlike the sibling ca-grid-weather-map, this project COMMITS data/, so a fresh
clone already has the full archive and the map opens instantly with no fetch.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from serve import Handler  # reuse the same handler that powers `python serve.py`

WINDOW_TITLE = "San Joaquin County spray timelapse"


def find_free_port() -> int:
    """Bind to port 0, let the OS pick, return the picked port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int) -> ThreadingHTTPServer:
    """Start serve.py's handler in a daemon thread. Returns the server."""
    handler = partial(Handler, directory=str(ROOT))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd


def run_fetch(reason: str) -> None:
    """Run fetch_data.py in-process, streaming its output."""
    print(f"[app] {reason}")
    result = subprocess.run(
        [sys.executable, str(ROOT / "fetch_data.py")],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if result.returncode != 0:
        print("[app] Fetch failed. Opening the archive that is already on disk.")


def ensure_data(refresh: bool) -> None:
    """data/ is committed, so normally there is nothing to do."""
    if refresh:
        run_fetch("Refreshing from the district's spray-alerts page...")
        return
    if not (ROOT / "data" / "operations.json").exists():
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

    ensure_data(args.refresh)

    port = find_free_port()
    httpd = start_server(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"[app] Serving {ROOT.name} on {url}")

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
