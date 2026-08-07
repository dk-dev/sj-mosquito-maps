"""
Tiny static-file + refresh server. Replaces `python -m http.server`.

  GET  /<path>              -> index.html and friends, from the program bundle
  GET  /data/<path>         -> the writable archive, wherever it actually lives
  POST /refresh             -> runs the fetcher IN-PROCESS, returns JSON
  POST /refresh?backfill=1  -> same, but also sweeps the Wayback Machine

Use:  python serve.py [port]   (default 8000)

TWO ROOTS, ONE HANDLER
----------------------
SimpleHTTPRequestHandler has exactly one ``directory``. This server needs two:
the read-only program bundle (index.html, icons) and the writable archive
(operations.json, shapes.geojson, manifest.json). In a dev checkout they nest
-- ``<repo>/data`` is inside ``<repo>`` -- which is why one root has always
been enough. In a frozen build the bundle is a temp extraction directory that
is deleted on exit and the archive is under %LOCALAPPDATA%, so they are
unrelated trees. ``translate_path`` below does the mapping; see its docstring
for the traversal argument.

NO SUBPROCESS
-------------
/refresh used to shell out to ``python fetch_data.py``. Inside a frozen onefile
app that is doubly broken: ``sys.executable`` is the app itself (so it would
relaunch the whole GUI) and ``fetch_data.py`` does not exist on disk at all.
The refresh therefore imports the fetcher and calls it directly.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import traceback
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sjmvcd import archive, paths

# Imported at module scope on purpose, for two reasons:
#   1. PyInstaller's static analysis has to SEE this import to pull the fetcher
#      and its dependency tree into the bundle. A lazy `import fetch_data`
#      inside the handler would build fine and then fail at the click of the
#      update button.
#   2. fetch_data reconfigures sys.stdout to UTF-8 at import time; doing that
#      here, once, rather than in the middle of a redirected-stdout block.
import fetch_data

# The bundle root is resolved once so translate_path can compare against it
# without re-resolving on every request.
BUNDLE_ROOT = paths.bundle_dir().resolve()

# A plain scrape finishes in well under a minute; a Wayback backfill walks
# ~30 archived snapshots and can run for many minutes. Budget separately so a
# normal refresh isn't held to the slow path's timeout.
TIMEOUT_REFRESH_S = 600
TIMEOUT_BACKFILL_S = 3600

# Single-flight. Two writers racing on data/operations.json could interleave
# archive writes, and the archive is not regenerable. Held for the whole life
# of a fetch -- including one that has blown its timeout and is still running
# in the background -- so a second click cannot start a concurrent writer.
_refresh_lock = threading.Lock()


SimpleHTTPRequestHandler.extensions_map[".geojson"] = "application/geo+json"

# The access log is bound to whatever stderr was at import time, and is
# deliberately NOT routed through sys.stderr at call time.
#
# run_fetch_in_process() swaps sys.stderr for a capture buffer, and the base
# handler logs every request through sys.stderr -- so a GET that completed
# while an update was running landed in the update's stderr_tail, and the
# frontend, which shows the LAST stderr line as the reason a refresh failed,
# would tell the user:
#     Update failed: 127.0.0.1 - - [...] "GET /data/operations.json" 200 -
# Binding the stream once here keeps request logging and fetch output in
# separate places, which is where they belonged all along. In a frozen
# windowed build this is the log file the runtime hook installed, so access
# lines still get recorded.
_ACCESS_LOG = sys.stderr


class _Tee:
    """
    Write-through capture: forwards to the real stream, keeps a copy.

    The refresh runs in-process, so the only way to fill ``stdout_tail`` is to
    intercept the fetcher's prints. Teeing rather than swallowing means a user
    who launched from a console still watches the run happen live.
    """

    def __init__(self, buffer: io.StringIO, passthrough) -> None:
        self._buffer = buffer
        self._passthrough = passthrough

    def write(self, text: str) -> int:
        self._buffer.write(text)
        if self._passthrough is not None:
            try:
                self._passthrough.write(text)
            except Exception:
                # A frozen windowed build has no console; sys.__stdout__ can be
                # None or a dead handle. Losing the echo must not lose the run.
                self._passthrough = None
        return len(text)

    def flush(self) -> None:
        if self._passthrough is not None:
            try:
                self._passthrough.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False


def run_fetch_in_process(backfill: bool) -> dict:
    """
    Run the fetcher in this thread and return the /refresh payload.

    Never raises: any exception the fetcher lets escape is turned into
    ``status: "error"`` with the traceback in ``stderr_tail``. That matters
    because fetch_data's own contract is already "a failed stage degrades the
    run to 'no new data' and leaves the committed archive exactly as it was" --
    so an error here means the archive on disk is untouched, which is the
    outcome we want and the one we report.
    """
    argv = ["--backfill"] if backfill else []
    out_buf, err_buf = io.StringIO(), io.StringIO()

    # NOTE: sys.stdout/stderr are process-global, so a concurrent request's
    # access-log line can land in these buffers. Harmless -- the tails are for
    # display only -- and the single-flight lock means it is never another
    # fetch's output.
    # Tee to the CURRENT streams, not sys.__stdout__/__stderr__. In a frozen
    # windowed build the originals are None -- there is no console -- and the
    # runtime hook redirects sys.stdout to the log file, not sys.__stdout__.
    # Teeing to the originals therefore dropped the passthrough on its first
    # write and threw away every line the fetcher printed, so the one operation
    # that touches the network and rewrites the archive was the only one that
    # logged nothing at all.
    prev_out, prev_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(out_buf, prev_out)
    sys.stderr = _Tee(err_buf, prev_err)

    t0 = time.time()
    returncode = 1
    try:
        # Cross-process guard. _refresh_lock above only covers threads inside
        # THIS process; a second copy of the frozen app resolves the same
        # archive and would otherwise merge concurrently.
        with archive.archive_lock(paths.data_dir()):
            returncode = fetch_data.main(argv)
    except archive.ArchiveBusy as exc:
        err_buf.write(f"{exc}\n")
        returncode = 1
    except SystemExit as exc:
        # argparse calls sys.exit on a bad flag; in-process that must not kill
        # the server thread.
        returncode = exc.code if isinstance(exc.code, int) else 1
    except BaseException:
        err_buf.write(traceback.format_exc())
        returncode = 1
    finally:
        sys.stdout, sys.stderr = prev_out, prev_err

    duration = round(time.time() - t0, 1)
    stdout_text, stderr_text = out_buf.getvalue(), err_buf.getvalue()

    payload = {
        "status": "ok" if returncode == 0 else "error",
        "returncode": returncode,
        "duration_s": duration,
        "backfill": backfill,
        # Tail the output so the client can show what happened without us
        # streaming megabytes back.
        "stdout_tail": "\n".join(stdout_text.splitlines()[-25:]),
        "stderr_tail": "\n".join(stderr_text.splitlines()[-10:]),
    }

    # Try to attach the manifest so the client can show counts. Read from the
    # writable archive, not the bundle.
    manifest_path = paths.data_dir() / "manifest.json"
    if manifest_path.exists():
        try:
            payload["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return payload


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        """Write the access log to the import-time stream, not sys.stderr.

        See the _ACCESS_LOG note above: routing through sys.stderr let request
        lines contaminate an in-flight fetch's captured output, and the UI
        surfaces the last such line as the failure reason.
        """
        try:
            _ACCESS_LOG.write("%s - - [%s] %s\n" % (
                self.address_string(), self.log_date_time_string(), fmt % args))
            _ACCESS_LOG.flush()
        except Exception:
            pass   # a windowed build with no console must not die on logging

    def do_GET(self):
        # A browser asks for /favicon.ico on every single launch. There is no
        # icon to serve, and letting it 404 writes a spurious error line into
        # the log a user would be asked to send in for support. 204 answers it
        # truthfully -- nothing here -- without the noise.
        if urlparse(self.path).path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:
        """
        Map a URL onto one of the two roots.

        ``/data/...`` resolves under ``paths.data_dir()``; everything else under
        ``paths.bundle_dir()``.

        TRAVERSAL: the sanitising is done by the base implementation, which is
        the part that has been audited by everyone. It strips the query and
        fragment, unquotes, ``posixpath.normpath``s, then walks the components
        and DISCARDS any that are ``.``, ``..``, or contain a path separator --
        so ``/data/../../../etc/passwd`` collapses to ``etc/passwd`` before it
        ever reaches us. We only ever re-root the already-sanitised *relative*
        result, and we do it with ``Path.joinpath`` on individual components,
        so no input can climb out of either tree. The one case we deliberately
        do not inherit is the Windows drive-letter quirk described below, which
        we close explicitly.
        """
        fs_path = super().translate_path(path)
        # Preserve the trailing slash super() adds for directory URLs; Path()
        # would strip it and send_head uses it for the redirect decision.
        trailing = "/" if fs_path.endswith(("/", os.sep)) else ""

        try:
            relative = Path(fs_path).relative_to(BUNDLE_ROOT)
        except ValueError:
            # The base implementation builds its result by os.path.join-ing
            # components onto self.directory, so it is normally guaranteed to
            # stay inside. The one hole is Windows: os.path.join(root, "C:")
            # returns "C:", because a bare drive letter is not a relative
            # component -- so "/C:/Windows/win.ini" would land outside the
            # tree. Anything that is not under the bundle root is refused
            # outright by pointing at a name that cannot exist.
            return str(BUNDLE_ROOT / "__forbidden__")

        parts = relative.parts
        # Case-insensitive on Windows deliberately. The filesystem is, so
        # "/DATA/manifest.json" would otherwise miss this branch, fall through
        # to the bundle root, and quietly serve the build-time SEED archive
        # instead of the user's live one -- a stale-data path that returns 200
        # rather than failing loudly.
        if parts and (parts[0] == "data"
                      or (sys.platform == "win32" and parts[0].lower() == "data")):
            # joinpath over the remaining components -- each already proven by
            # the base implementation not to be '..' and not to contain a path
            # separator, so this cannot climb out of the data root either.
            return str(paths.data_dir().joinpath(*parts[1:])) + trailing
        return fs_path

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/refresh":
            self.send_error(404, "only POST /refresh is supported")
            return

        backfill = parse_qs(parsed.query).get("backfill", ["0"])[0] not in ("0", "", "false")

        # Prevent overlapping fetches; one user clicks twice or a watcher fires.
        if not _refresh_lock.acquire(blocking=False):
            self._json(409, {"status": "busy", "message": "a refresh is already running"})
            return

        limit = TIMEOUT_BACKFILL_S if backfill else TIMEOUT_REFRESH_S
        result: dict = {}

        def _worker() -> None:
            try:
                result.update(run_fetch_in_process(backfill))
            finally:
                # Released by the worker, not by the request thread. If we time
                # out below, the fetch is still running and still writing; the
                # lock must stay held until it is genuinely finished or a second
                # click would start a concurrent writer on the archive.
                _refresh_lock.release()

        # A thread rather than a straight call so a hung upstream still lets the
        # endpoint answer. There is no way to kill a Python thread, so a timeout
        # reports "still running" instead of pretending it was cancelled.
        worker = threading.Thread(target=_worker, name="refresh", daemon=True)
        worker.start()
        worker.join(limit)

        if worker.is_alive():
            self._json(504, {
                "status": "error",
                "backfill": backfill,
                "message": f"fetch exceeded {limit}s and is still running in the "
                           f"background; the existing archive is unchanged so far",
            })
            return

        self._json(200 if result.get("status") == "ok" else 500, result)

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # SimpleHTTPRequestHandler caches forever by default. That was already
    # wrong for data/, which changes under the page; it is just as wrong for
    # index.html, which is the file being edited while this server runs.
    #
    # A cached index.html does not merely show stale pixels -- it silently
    # invalidates testing. A change can be saved, the server can serve it, the
    # page can be reloaded, and the browser can still be running the previous
    # JavaScript, so a fix looks like it did not work and a bug looks fixed.
    # Both happened here. Everything this server hands out is either the
    # archive or the app itself, and neither is worth caching for a local run.
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()


def make_handler():
    """Handler factory bound to the bundle root. Shared with app.py."""
    return partial(Handler, directory=str(BUNDLE_ROOT))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    # Cheap in dev (source and destination are the same directory, detected and
    # skipped); the thing that makes a frozen first run work at all.
    copied, data_root = paths.seed_data_dir()
    if copied:
        print(f"seeded {copied} archive file(s) into {data_root}")

    # 127.0.0.1, not "" (all interfaces). This server hands out the whole
    # bundle root, which in a dev checkout is the repository -- source, .venv
    # and all -- and exposes a POST that drives scrapes against the district
    # and Google from this machine's address. None of that should be reachable
    # from the rest of the network. app.py already binds the loopback only.
    with ThreadingHTTPServer(("127.0.0.1", port), make_handler()) as httpd:
        print(f"serving {BUNDLE_ROOT} on http://localhost:{port}/")
        print(f"  /data/* -> {data_root}")
        print(f"  POST http://localhost:{port}/refresh             re-scrape the live page")
        print(f"  POST http://localhost:{port}/refresh?backfill=1  also sweep the Wayback Machine")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
