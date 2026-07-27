"""Wayback Machine backfill for the district's spray-alerts page.

WHY THIS MODULE EXISTS
----------------------
The San Joaquin County Mosquito & Vector Control District publishes each spray
operation on a single page, and that page only retains roughly two months of
history. Once an operation scrolls off the "Past Completed Spray Operations"
list it is gone from the public web forever -- the district publishes no
archive, no feed, and no per-year page.

The Internet Archive, however, has been capturing that URL since 2019. Each
capture is a frozen copy of the page *as it looked that day*, which means each
capture carries its own ~2-month trailing window of operations. Walking the
captures in order therefore reconstructs a multi-year history that no longer
exists anywhere on the live web.

The other half of the trick: an archived capture gives us the (date, area,
Google My Maps ``mid``) tuples, and Google *still serves the KML* for those
map IDs today -- including maps last referenced in 2020. So the Archive
supplies the schedule and Google supplies the geometry. Recon resolved all 321
recovered map IDs against Google and 320 returned a polygon.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not parse. Parsing lives in :mod:`sjmvcd.parse` and is shared verbatim
with the live-page scrape, because an archived capture is byte-for-byte the
same DotNetNuke markup the live site served that day. Any parser divergence
between the two paths would silently corrupt the archive, so there is exactly
one parser and this module just feeds HTML into it with a different ``source``
label.

COST AND SCHEDULING
-------------------
A full sweep is ~33 archived captures at a mandatory 1 s spacing (see
``WAYBACK_MIN_SPACING`` below), i.e. roughly a minute of wall time, plus
whatever new map IDs it uncovers for the shape fetcher. That is far too much
load to impose on the Internet Archive every six hours for data that changes a
handful of times a year, so the orchestrator runs it only behind an explicit
``--backfill`` flag (weekly cron or manual dispatch). Everything here is
idempotent: re-running merges cleanly and produces no duplicate operations.

PUBLIC API
----------
``list_snapshots``      -- enumerate usable capture timestamps from the CDX API
``snapshot_url``        -- build the raw-content URL for one capture
``backfill_operations`` -- fetch + parse a list of captures into Operation dicts
``run_backfill``        -- convenience wrapper that never raises (for the CLI /
                           orchestrator ``--backfill`` path)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlencode

from . import http as sjhttp
from . import parse as sjparse

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: The page we are reconstructing. Must match the URL sjmvcd.scrape uses for
#: the live fetch, otherwise the archive and the live scrape drift apart.
PAGE_URL = "https://www.sjmosquito.org/News-Spray-Alerts/Spray-Alerts-Maps"

#: CDX wants a bare (scheme-less) URL. It canonicalises to an internal
#: ``urlkey``, so this single query already returns every host/scheme/port
#: variant the site has ever been captured under (http://, https://, with and
#: without ``www``, and the ``:80`` forms). Issuing one query per variant is
#: wasted load -- recon confirmed the variants are already folded in.
CDX_TARGET = "sjmosquito.org/News-Spray-Alerts/Spray-Alerts-Maps"
CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"

#: Prefix for a capture. The ``id_`` modifier appended to the timestamp asks
#: for the ORIGINAL bytes rather than the rewritten "replay" page. Without it
#: the Archive injects its own toolbar markup and rewrites every href through
#: ``/web/<ts>/`` -- which would still parse, but would mangle the Google Maps
#: links we extract ``mid`` from. Always use ``id_``.
WAYBACK_REPLAY_PREFIX = "https://web.archive.org/web/"
SNAPSHOT_MODIFIER = "id_"

#: Captures older than this contain zero Google My Maps links: before
#: 2020-08-14 the district linked static PDF maps (all of which now 404), and
#: the 2019/early-2020 DotNetNuke captures list no operations at all. Fetching
#: them costs ~10 requests and yields nothing, so they are skipped by default.
#: Pass ``include_empty_era=True`` to walk them anyway.
EARLIEST_USEFUL_TIMESTAMP = "20200814043508"

#: web.archive.org throttles aggressively and *reproducibly*. Measured: 1.0 s
#: spacing sustained 12/12 successful fetches; 0 s spacing gave 7 successes
#: then five consecutive ConnectionErrors (WinError 10061, connection refused)
#: -- the host stops accepting TCP entirely rather than returning 429. It
#: recovers after ~20 s. So: space requests by 1 s, and on failure cool off
#: hard rather than hammering.
WAYBACK_MIN_SPACING = 1.0
WAYBACK_TIMEOUT = 90
WAYBACK_ATTEMPTS = 3
WAYBACK_COOLDOWN = 20.0

#: The CDX endpoint is a separate, lighter service; ordinary retry is fine.
#: It is however genuinely slow -- a full enumeration of this URL measured
#: ~50 s of server-side thinking before the first byte -- so the timeout is
#: generous rather than tight.
CDX_TIMEOUT = 120
CDX_RETRIES = 3

#: HTTP statuses that mean "this capture will never load", so the cooldown
#: loop in :func:`fetch_snapshot` should give up immediately instead of
#: sleeping 20 s three times to re-confirm a permanent answer.
PERMANENT_STATUSES = frozenset({400, 403, 404, 410, 451})

#: A capture that parses to fewer bytes than this is almost certainly an
#: Archive error page ("Got an HTTP 302 response at crawl time") rather than a
#: real capture. Recon's smallest genuine capture was ~41 KB; the 301 stub was
#: 367 bytes. Anything under 2 KB is treated as a failed fetch.
MIN_PLAUSIBLE_SNAPSHOT_BYTES = 2048

#: Fields that a *later* capture is allowed to overwrite on an operation we
#: already recorded from an earlier capture. An operation legitimately
#: transitions scheduled -> complete and moves current -> past between
#: captures, so the newest observation of these is the truthful one. Every
#: other field (crucially ``id``, ``source`` and ``first_seen``) is frozen at
#: first observation so provenance points at the capture that actually
#: rescued the record.
MUTABLE_FIELDS = ("status", "status_text", "section")

_TIMESTAMP_RE = re.compile(r"^\d{4,14}$")


# --------------------------------------------------------------------------
# CDX enumeration
# --------------------------------------------------------------------------


def _normalize_since(since: str | None) -> str | None:
    """Coerce a user-supplied date/timestamp into a CDX-comparable prefix.

    Accepts ``'2025'``, ``'2025-08'``, ``'2025-08-11'``, ``'20250811'`` or a
    full 14-digit ``'20250811141635'``. Returns a digits-only string that can
    be compared lexicographically against a CDX timestamp (CDX timestamps are
    fixed-width ``YYYYMMDDhhmmss``, so a left-anchored prefix comparison is a
    correct chronological comparison once both sides are zero-padded).

    Returns ``None`` for ``None`` / empty input, meaning "no lower bound".
    """
    if since is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(since))
    if not digits:
        return None
    if not _TIMESTAMP_RE.match(digits):
        raise ValueError(f"unparseable 'since' value: {since!r}")
    # Pad to full width so string comparison is chronological: '2025' becomes
    # '20250000000000', which sorts before every real 2025 capture.
    return digits.ljust(14, "0")


def _cdx_query_url() -> str:
    """Build the CDX request.

    Deliberate choices:

    * ``fl=timestamp,statuscode,digest`` -- we need the digest to drop
      byte-identical re-captures (see :func:`list_snapshots`).
    * **no** ``collapse=`` parameter. CDX-side collapsing operates on adjacent
      rows only and would hide the digest information we want to act on. We
      collapse in code instead, where the rules are explicit and testable.
    * ``filter=statuscode:200`` is *not* used either: a server-side filter
      makes a redirect capture vanish without trace, and we would rather see
      it in the raw rows and log why it was dropped.
    """
    params = {
        "url": CDX_TARGET,
        "output": "json",
        "fl": "timestamp,statuscode,digest",
    }
    return f"{CDX_ENDPOINT}?{urlencode(params)}"


def list_snapshots(
    limit: int | None = None,
    *,
    since: str | None = None,
    include_empty_era: bool = False,
) -> list[str]:
    """Return usable Wayback capture timestamps for the spray-alerts page.

    The returned list is **ascending** (oldest first). Ascending order matters
    downstream: :func:`backfill_operations` gives the first capture that
    produced a record ownership of its ``source``/``first_seen``, so walking
    forward in time makes provenance point at the *earliest* observation,
    which is the intuitively correct answer to "where did this record come
    from".

    Filtering pipeline, in order:

    1. Drop rows whose ``statuscode`` is not ``200``. The only non-200 row in
       the history is a 301 at ``20231014142111`` -- the site's plain-HTTP to
       HTTPS canonicalisation, not a page move. It carries no content and the
       Archive resolves it to the 200 capture 46 seconds later, which we keep
       anyway. Nothing is lost.
    2. Drop rows before ``since`` (if given) and before
       :data:`EARLIEST_USEFUL_TIMESTAMP` (unless ``include_empty_era``).
    3. Collapse to one capture per calendar day, keeping the earliest.
    4. Drop captures whose content ``digest`` we have already accepted --
       re-fetching identical bytes costs a request and yields nothing.

       Measured caveat: on this URL that rule currently fires **zero** times.
       Pairs that look like duplicates by response length (``20201106144111``
       / ``20201110160147`` are both 99076 bytes, ``20210902093910`` /
       ``20210902182258`` are both 91825) are *not* byte-identical -- their
       SHA-1s differ, because DotNetNuke stamps a fresh ViewState into every
       response. The rule is kept because it is correct and free, but it must
       not be relied on as the thing that keeps the walk short; the per-day
       collapse in step 3 is what actually does that work.

    :param limit: Keep at most this many captures. Truncation takes the
        **most recent** ``limit`` captures (still returned ascending). The
        rationale: the captures nearest the present fill the gap immediately
        behind the live page's two-month window, so a truncated run is still
        contiguous with the live data. Raising the limit on a later run walks
        progressively further back in time, and because the merge is
        idempotent the archive simply deepens.
    :param since: Optional lower bound -- ``'2025'``, ``'2025-08-11'`` or a
        full ``YYYYMMDDhhmmss`` timestamp. Useful for a cheap incremental
        sweep ("anything captured since the last successful backfill").
    :param include_empty_era: Also return the pre-2020-08 captures that
        contain no Google My Maps links. Off by default; they are pure cost.
    :raises Exception: propagates if the CDX API cannot be reached at all.
        The caller (``fetch_data.py``) is responsible for isolating that
        failure so the live scrape still produces output -- or it can use
        :func:`run_backfill`, which swallows it.
    """
    raw = sjhttp.get_text(_cdx_query_url(), timeout=CDX_TIMEOUT, retries=CDX_RETRIES)

    rows = json.loads(raw)
    if not rows:
        log.warning("CDX returned no rows for %s", CDX_TARGET)
        return []

    # First row is the header (['timestamp', 'statuscode', 'digest']).
    header, data_rows = rows[0], rows[1:]
    try:
        i_ts = header.index("timestamp")
        i_code = header.index("statuscode")
        i_digest = header.index("digest")
    except ValueError as exc:  # pragma: no cover - CDX contract change
        raise RuntimeError(f"unexpected CDX header {header!r}") from exc

    floor = _normalize_since(since)
    if not include_empty_era:
        # Take whichever floor is later: an explicit 'since' should be able to
        # narrow the window but not to re-open the empty pre-2020 era.
        floor = max(filter(None, (floor, EARLIEST_USEFUL_TIMESTAMP)))

    kept: list[str] = []
    seen_days: set[str] = set()
    seen_digests: set[str] = set()
    n_non200 = 0
    n_before_floor = 0
    n_same_day = 0
    n_dup_digest = 0

    for row in sorted(data_rows, key=lambda r: r[i_ts]):
        ts = row[i_ts]
        code = row[i_code]
        digest = row[i_digest]

        if code != "200":
            n_non200 += 1
            log.debug("CDX skip %s: statuscode=%s", ts, code)
            continue
        if floor and ts < floor:
            n_before_floor += 1
            continue

        day = ts[:8]
        if day in seen_days:
            n_same_day += 1
            log.debug("CDX skip %s: another capture already kept for %s", ts, day)
            continue
        if digest in seen_digests:
            n_dup_digest += 1
            log.debug("CDX skip %s: identical content digest %s", ts, digest)
            continue

        seen_days.add(day)
        seen_digests.add(digest)
        kept.append(ts)

    log.info(
        "CDX: %d rows -> %d usable captures "
        "(dropped %d non-200, %d before floor, %d same-day, %d duplicate-digest)",
        len(data_rows),
        len(kept),
        n_non200,
        n_before_floor,
        n_same_day,
        n_dup_digest,
    )

    if limit is not None and limit >= 0 and len(kept) > limit:
        # Tail, not head -- see the docstring for why recency wins.
        dropped = len(kept) - limit
        kept = kept[-limit:] if limit else []
        log.info("limit=%s: walking the %d most recent captures (%d older skipped)",
                 limit, len(kept), dropped)

    return kept


def snapshot_url(timestamp: str) -> str:
    """Return the URL that serves a capture's ORIGINAL, unrewritten bytes.

    The ``id_`` modifier is load-bearing. Without it the Archive returns its
    replay wrapper: an injected toolbar, injected scripts, and every URL in
    the document rewritten to point back into ``web.archive.org``. The Google
    My Maps hrefs we mine ``mid`` from would come back as
    ``/web/<ts>/https://www.google.com/maps/d/edit?mid=...`` -- still
    matchable, but only by accident, and the rewriting rules have changed over
    the Archive's lifetime. ``id_`` gives us exactly what the district's
    server sent that day, which is what the parser was written against.
    """
    return f"{WAYBACK_REPLAY_PREFIX}{timestamp}{SNAPSHOT_MODIFIER}/{PAGE_URL}"


# --------------------------------------------------------------------------
# Snapshot fetching
# --------------------------------------------------------------------------


def _cache_path(cache_dir: str | Path | None, timestamp: str) -> Path | None:
    """Resolve the on-disk cache path for a capture, or ``None`` if disabled."""
    if not cache_dir:
        return None
    return Path(cache_dir) / f"{timestamp}.html"


def fetch_snapshot(
    timestamp: str,
    *,
    cache_dir: str | Path | None = None,
) -> str | None:
    """Fetch one capture's original HTML. Returns ``None`` on failure.

    This never raises. A dead capture must cost us that capture and nothing
    else -- the whole point of the backfill is that it degrades gracefully.

    Retry policy is tuned to how web.archive.org actually misbehaves: it does
    not politely 429, it drops the TCP connection outright once you exceed its
    rate budget, and it stays that way for roughly twenty seconds. So the
    backoff here is a flat, generous cooldown rather than a fast exponential
    ramp -- retrying quickly just extends the ban.

    Captures are immutable (the Archive never rewrites stored bytes), so when
    ``cache_dir`` is set the HTML is cached forever and a run interrupted
    halfway through resumes for free.
    """
    path = _cache_path(cache_dir, timestamp)
    if path and path.is_file():
        try:
            cached = path.read_text(encoding="utf-8", errors="replace")
            if len(cached) >= MIN_PLAUSIBLE_SNAPSHOT_BYTES:
                log.debug("cache hit %s (%d bytes)", timestamp, len(cached))
                return cached
            log.debug("ignoring implausibly small cached capture %s", timestamp)
        except OSError as exc:  # pragma: no cover - unreadable cache is not fatal
            log.warning("cache read failed for %s: %s", timestamp, exc)

    url = snapshot_url(timestamp)
    for attempt in range(1, WAYBACK_ATTEMPTS + 1):
        # Space every outbound request, including the first: the caller is
        # looping over dozens of captures and the spacing is what keeps the
        # Archive answering at all.
        sjhttp.polite_sleep(WAYBACK_MIN_SPACING)
        try:
            html = sjhttp.get_text(url, timeout=WAYBACK_TIMEOUT, retries=1)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            log.warning(
                "snapshot %s attempt %d/%d failed: %s",
                timestamp, attempt, WAYBACK_ATTEMPTS, exc,
            )
            # Duck-typed rather than importing requests: any exception carrying
            # a response with a permanent status is a settled answer. Sleeping
            # 20 s to ask a 404 twice more is rude and pointless.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in PERMANENT_STATUSES:
                log.warning(
                    "snapshot %s returned a permanent HTTP %s; not retrying",
                    timestamp, status,
                )
                return None
            if attempt < WAYBACK_ATTEMPTS:
                log.info("cooling off %.0fs before retrying %s", WAYBACK_COOLDOWN, timestamp)
                time.sleep(WAYBACK_COOLDOWN)
            continue

        if html is None or len(html) < MIN_PLAUSIBLE_SNAPSHOT_BYTES:
            # Typically an Archive stub such as "Got an HTTP 302 response at
            # crawl time" -- a 200 wrapper around no content. Treat as failure
            # so it is retried and, if persistent, reported.
            log.warning(
                "snapshot %s attempt %d/%d returned only %d bytes; treating as failure",
                timestamp, attempt, WAYBACK_ATTEMPTS, len(html or ""),
            )
            if attempt < WAYBACK_ATTEMPTS:
                time.sleep(WAYBACK_COOLDOWN)
            continue

        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(html, encoding="utf-8")
            except OSError as exc:  # pragma: no cover - cache is best-effort
                log.warning("cache write failed for %s: %s", timestamp, exc)
        return html

    return None


# --------------------------------------------------------------------------
# Backfill
# --------------------------------------------------------------------------


def _merge_observation(kept: dict[str, Any], later: dict[str, Any]) -> None:
    """Fold a later observation of an already-seen operation into ``kept``.

    Called with ``later`` strictly newer than ``kept`` (the caller walks
    captures ascending). Two rules:

    * ``MUTABLE_FIELDS`` are overwritten -- an operation really does move from
      ``scheduled`` to ``complete`` and from the ``current`` list to the
      ``past`` list as the season progresses, and the newest capture that saw
      it holds the truth.
    * Any field that is ``None``/empty in ``kept`` but populated in ``later``
      is filled in. This is a strict improvement and never loses data: it
      recovers, for example, a time window that one capture omitted and a
      later one spelled out.

    Everything else -- notably ``id``, ``source`` and ``first_seen`` -- is
    frozen at first observation.
    """
    for field in MUTABLE_FIELDS:
        if field in later:
            kept[field] = later[field]

    for field, value in later.items():
        if field in MUTABLE_FIELDS or field in ("source", "first_seen", "id"):
            continue
        if kept.get(field) in (None, "", []) and value not in (None, "", []):
            kept[field] = value


def _sort_key(op: dict[str, Any]) -> tuple[str, str, str]:
    """Stable ordering for the returned list: (date, area_name, mid).

    Deterministic ordering is what keeps the committed ``operations.json``
    diff small -- without it, dict iteration order leaks into the file and
    every run rewrites the whole archive.
    """
    return (op.get("date") or "", op.get("area_name") or "", op.get("mid") or "")


def backfill_operations(
    snapshots: list[str],
    *,
    cache_dir: str | Path | None = None,
    errors: list[str] | None = None,
    per_snapshot: Callable[[str, int, int], None] | None = None,
) -> list[dict]:
    """Fetch each capture, parse it, and return deduplicated Operation dicts.

    Parsing is delegated wholesale to :func:`sjmvcd.parse.parse_operations`
    with ``source=f"wayback:{timestamp}"``. There is intentionally no
    archive-specific parsing here -- see the module docstring.

    Deduplication uses the contract's operation ``id`` (``"<date>|<mid>"``).
    This matters more than it looks: recon found 898 distinct (date, mid)
    pairs across the capture set of which only 299 appear in a single capture
    -- 599 appear in two to eight. Note also that a map ID legitimately
    recurs on *different* dates (the same area sprayed repeatedly), so the key
    must be the pair and never the ``mid`` alone.

    :param snapshots: Capture timestamps. Sorted ascending defensively, so a
        caller that hands them over in any order still gets earliest-wins
        provenance.
    :param cache_dir: Optional directory for caching capture HTML. Captures
        are immutable, so this makes an interrupted run resumable for free.
    :param errors: Optional list; one human-readable string is appended for
        each capture that could not be fetched or parsed. Feeds straight into
        ``manifest.errors``.
    :param per_snapshot: Optional progress callback invoked as
        ``(timestamp, n_parsed, n_new)`` after each capture.
    :returns: Operation dicts per the data contract, sorted by
        ``(date, area_name, mid)``.
    """
    sink = errors if errors is not None else []
    merged: dict[str, dict[str, Any]] = {}

    ordered = sorted(set(snapshots))
    log.info("backfill: walking %d capture(s)", len(ordered))

    for index, timestamp in enumerate(ordered, start=1):
        source = f"wayback:{timestamp}"

        # ---- fetch (isolated) ------------------------------------------
        try:
            html = fetch_snapshot(timestamp, cache_dir=cache_dir)
        except Exception as exc:  # noqa: BLE001 - defence in depth
            msg = f"{source}: unexpected fetch error: {exc}"
            log.warning("%s", msg)
            sink.append(msg)
            continue

        if html is None:
            msg = f"{source}: fetch failed after {WAYBACK_ATTEMPTS} attempts; skipped"
            log.warning("%s", msg)
            sink.append(msg)
            continue

        # ---- parse (isolated) ------------------------------------------
        # A layout the parser has never seen (the page has been through at
        # least three) must cost us one capture, not the whole sweep.
        try:
            ops = sjparse.parse_operations(html, source=source)
        except Exception as exc:  # noqa: BLE001 - see above
            msg = f"{source}: parse failed: {exc.__class__.__name__}: {exc}"
            log.warning("%s", msg)
            sink.append(msg)
            continue

        # ---- merge ------------------------------------------------------
        n_new = 0
        for op in ops:
            key = op.get("id")
            if not key:
                # Defensive: an operation with no id cannot be deduplicated,
                # so it would multiply on every re-run. Drop it loudly.
                log.debug("%s: dropping operation with no id: %r", source, op)
                continue
            if key in merged:
                _merge_observation(merged[key], op)
            else:
                merged[key] = dict(op)
                n_new += 1

        log.info(
            "[%2d/%2d] %s -> %3d operations parsed, %3d new (running total %d)",
            index, len(ordered), timestamp, len(ops), n_new, len(merged),
        )
        if per_snapshot is not None:
            try:
                per_snapshot(timestamp, len(ops), n_new)
            except Exception as exc:  # noqa: BLE001 - a bad callback is not fatal
                log.debug("per_snapshot callback raised: %s", exc)

    result = sorted(merged.values(), key=_sort_key)
    log.info("backfill: %d unique operations recovered", len(result))
    return result


def run_backfill(
    *,
    limit: int | None = None,
    since: str | None = None,
    cache_dir: str | Path | None = None,
    errors: list[str] | None = None,
    include_empty_era: bool = False,
) -> list[dict]:
    """Enumerate and walk the archive, swallowing every failure.

    This is the entry point the orchestrator's ``--backfill`` flag should
    call. It exists so a Wayback outage degrades the run to "no historical
    operations this time" instead of taking down the live scrape and the
    shape fetch with it. Anything that goes wrong is recorded in ``errors``
    and reported in the manifest.
    """
    sink = errors if errors is not None else []
    try:
        snapshots = list_snapshots(
            limit, since=since, include_empty_era=include_empty_era
        )
    except Exception as exc:  # noqa: BLE001 - the whole point of this wrapper
        msg = f"wayback: CDX enumeration failed: {exc.__class__.__name__}: {exc}"
        log.warning("%s", msg)
        sink.append(msg)
        return []

    if not snapshots:
        log.info("wayback: no captures matched; nothing to backfill")
        return []

    return backfill_operations(snapshots, cache_dir=cache_dir, errors=sink)


# --------------------------------------------------------------------------
# CLI -- run with: python -m sjmvcd.backfill
# --------------------------------------------------------------------------


def _summarize(ops: Sequence[dict]) -> dict[str, Any]:
    """Compute the headline numbers a human wants after a sweep."""
    dates = sorted(o["date"] for o in ops if o.get("date"))
    mids = {o["mid"] for o in ops if o.get("mid")}
    sources: dict[str, int] = {}
    for op in ops:
        sources[op.get("source", "?")] = sources.get(op.get("source", "?"), 0) + 1
    return {
        "operations": len(ops),
        "unique_mids": len(mids),
        "unique_dates": len(set(dates)),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "by_source": dict(sorted(sources.items())),
    }


def main(argv: Iterable[str] | None = None) -> int:
    """Standalone driver, for operators and for verifying this module alone."""
    ap = argparse.ArgumentParser(
        prog="python -m sjmvcd.backfill",
        description="Walk the Wayback Machine for historical spray operations.",
    )
    ap.add_argument("--limit", type=int, default=None,
                    help="walk at most N captures (the most recent N)")
    ap.add_argument("--since", default=None,
                    help="only captures on/after this date (2025, 2025-08, 20250811)")
    ap.add_argument("--include-empty-era", action="store_true",
                    help="also walk pre-2020-08 captures (they contain no map IDs)")
    ap.add_argument("--cache-dir", default=None,
                    help="cache capture HTML here (captures are immutable)")
    ap.add_argument("--list-only", action="store_true",
                    help="print the capture timestamps and exit without fetching")
    ap.add_argument("--out", default=None,
                    help="write the recovered operations to this JSON file")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )

    if args.list_only:
        for ts in list_snapshots(args.limit, since=args.since,
                                 include_empty_era=args.include_empty_era):
            print(ts)
        return 0

    errors: list[str] = []
    started = time.time()
    ops = run_backfill(
        limit=args.limit,
        since=args.since,
        cache_dir=args.cache_dir,
        errors=errors,
        include_empty_era=args.include_empty_era,
    )
    elapsed = time.time() - started

    summary = _summarize(ops)
    summary["elapsed_seconds"] = round(elapsed, 1)
    summary["errors"] = errors
    print(json.dumps(summary, indent=2))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps({"operations": ops}, indent=2), encoding="utf-8"
        )
        print(f"wrote {len(ops)} operations to {args.out}", file=sys.stderr)

    # Exit non-zero only if we recovered nothing at all AND hit errors -- a
    # partial sweep is a success, because the merge is append-only.
    return 1 if (not ops and errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
