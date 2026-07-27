"""Polite HTTP helpers shared by every sjmvcd module.

Everything this project touches is a public server run by somebody else: the
San Joaquin County MVCD web site, Google My Maps, and the Internet Archive.
None of them owe us anything, so this module bakes the good-citizen behaviour
in at the transport layer rather than trusting each caller to remember it:

* a descriptive ``User-Agent`` that says who we are and what we are doing, so
  an admin reading their logs can identify (and if necessary block) us without
  guesswork;
* a hard floor on the interval between outbound requests (see
  ``MIN_REQUEST_INTERVAL``), enforced process-wide;
* retries with exponential backoff and jitter, honouring ``Retry-After``;
* no retry on client errors that will never succeed (404, 403, ...), so a dead
  map id fails fast instead of costing three round trips.

Public API (fixed by the project's module contract)::

    USER_AGENT: str
    get_text(url, *, timeout=60, retries=3) -> str        # raises on final failure
    get_text_or_none(url, **kw) -> str | None
    polite_sleep(seconds=0.4) -> None
"""

from __future__ import annotations

import logging
import random
import threading
import time

import requests

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

#: Identifies the project, its purpose, and where to complain.  Keep the
#: "archiver" wording: it tells a server operator this is a low-volume,
#: read-only, public-data crawl rather than a scraper farming their content.
USER_AGENT = (
    "sj-mosquito-maps/1.0 "
    "(+https://github.com/dk-dev/sj-mosquito-maps; "
    "public-data archiver for San Joaquin County MVCD spray alerts; "
    "low volume, read-only) "
    "python-requests"
)

#: Default polite pause callers are expected to take between requests.
DEFAULT_SLEEP = 0.4

#: Process-wide floor on the gap between two outbound requests.  This is a
#: safety net, not a replacement for :func:`polite_sleep`: a caller that
#: forgets to sleep still cannot exceed 1/MIN_REQUEST_INTERVAL requests per
#: second.  Kept below DEFAULT_SLEEP so a well-behaved caller's own pause
#: dominates and we do not sleep twice for the same request.
MIN_REQUEST_INTERVAL = 0.25

#: Seconds to wait before the first retry.  Doubles each attempt.
BACKOFF_BASE = 1.5

#: Upper bound on a single backoff sleep, including a server ``Retry-After``.
BACKOFF_MAX = 30.0

#: HTTP status codes worth retrying.  Everything else in 4xx is a permanent
#: answer -- retrying a 404 map id just wastes the upstream's time.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})

# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

_session_lock = threading.Lock()
_session: requests.Session | None = None

_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _get_session() -> requests.Session:
    """Return the shared :class:`requests.Session` (created on first use).

    Reusing one session keeps TCP/TLS connections alive across the ~150
    requests a full backfill makes, which is both faster for us and cheaper
    for the servers we are talking to.
    """
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    # Ask for text; some CDNs vary their response on this.
                    "Accept": "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    # Explicitly do not send Connection: close -- keep-alive is
                    # the polite default and requests handles it for us.
                }
            )
            _session = s
        return _session


def _throttle() -> None:
    """Block until at least ``MIN_REQUEST_INTERVAL`` has elapsed since the last
    outbound request.  Cheap no-op when the caller is already pacing itself."""
    global _last_request_at
    with _throttle_lock:
        now = time.monotonic()
        wait = MIN_REQUEST_INTERVAL - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_request_at = now


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """Parse a ``Retry-After`` header expressed in seconds.

    The HTTP-date form is deliberately not supported: upstreams here use the
    numeric form, and a mis-parsed date could park the run for hours.
    """
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw.strip()), BACKOFF_MAX))
    except ValueError:
        return None


def _decode(resp: requests.Response) -> str:
    """Decode a response body to ``str`` with a sane encoding fallback.

    ``requests`` falls back to ISO-8859-1 for ``text/*`` responses that carry
    no ``charset`` parameter (an old HTTP/1.1 rule).  Google's KML endpoint is
    UTF-8 and does not always declare it, so mojibake would silently land in
    place-names.  When the header does not state a charset we prefer UTF-8 and
    only fall back to chardet's guess if UTF-8 does not decode.
    """
    content_type = resp.headers.get("Content-Type", "")
    if "charset=" not in content_type.lower():
        try:
            return resp.content.decode("utf-8")
        except UnicodeDecodeError:
            resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def polite_sleep(seconds: float = DEFAULT_SLEEP) -> None:
    """Pause between requests to avoid hammering an upstream.

    Callers should invoke this between iterations of any fetch loop.  Negative
    or zero values are ignored so ``polite_sleep(0)`` is a legal "no pause".
    """
    if seconds and seconds > 0:
        time.sleep(seconds)


def get_text(url: str, *, timeout: int = 60, retries: int = 3) -> str:
    """Fetch ``url`` and return the decoded body.

    Parameters
    ----------
    url:
        Absolute URL to fetch.
    timeout:
        Per-attempt timeout in seconds, passed straight to ``requests``.
    retries:
        Number of *additional* attempts after the first one.  ``retries=3``
        means up to four requests total.

    Returns
    -------
    str
        The decoded response body.

    Raises
    ------
    requests.RequestException
        If every attempt failed.  The exception from the final attempt is
        re-raised so the caller sees the real cause (timeout, DNS, HTTP
        status, ...).  Use :func:`get_text_or_none` when a failure should be
        recorded and stepped over rather than propagated.
    """
    attempts = max(1, retries + 1)
    session = _get_session()
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        _throttle()
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            # Network-level failure (DNS, connect, read timeout, TLS).  Always
            # worth one more try -- these are usually transient.
            last_exc = exc
            log.warning("GET %s attempt %d/%d failed: %s", url, attempt, attempts, exc)
        else:
            if resp.status_code == 200:
                return _decode(resp)

            retryable = resp.status_code in RETRY_STATUS
            last_exc = requests.HTTPError(
                f"HTTP {resp.status_code} for {url}", response=resp
            )
            log.warning(
                "GET %s attempt %d/%d -> HTTP %d%s",
                url,
                attempt,
                attempts,
                resp.status_code,
                "" if retryable else " (not retryable)",
            )
            if not retryable:
                # A permanent answer: stop burning the upstream's capacity.
                raise last_exc

            # Respect an explicit Retry-After before falling back to backoff.
            hinted = _retry_after_seconds(resp)
            if hinted is not None and attempt < attempts:
                time.sleep(hinted)
                continue

        if attempt < attempts:
            # Exponential backoff with jitter: 1.5s, 3s, 6s (+/- up to 40%).
            delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_MAX)
            delay *= 0.8 + 0.4 * random.random()
            time.sleep(delay)

    assert last_exc is not None  # unreachable: the loop runs at least once
    raise last_exc


def get_text_or_none(url: str, **kw) -> str | None:
    """Like :func:`get_text` but return ``None`` instead of raising.

    This is the failure-isolating variant used by the batch fetchers: one dead
    map id or one 404 Wayback snapshot must not abort a run that still has 50
    good records to write.  The failure is logged at WARNING level; the caller
    is responsible for counting it into the manifest.
    """
    try:
        return get_text(url, **kw)
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
        log.warning("giving up on %s: %s", url, exc)
        return None
