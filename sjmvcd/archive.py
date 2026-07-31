"""
Archive merge layer.

This module owns the two committed data files that ARE the project:
``data/operations.json`` and ``data/shapes.geojson``. The district's own page
only retains roughly two months of spray operations; once an entry rolls off,
the copy in this repository is the only remaining public record of it.

Everything here is therefore written defensively around a single invariant:

    A RUN MAY ONLY EVER ADD TO THE ARCHIVE.

Concretely:

  * :func:`merge_operations` never drops an id it was handed in ``existing``.
    It refreshes only the fields that legitimately change over an operation's
    life (status / status_text / section) and always preserves ``first_seen``.

  * :func:`load_operations` raises when the file exists but cannot be read,
    instead of degrading to an empty list. Returning ``[]`` for a corrupt or
    half-written archive would let the very next write replace a year of
    history with today's much smaller page scrape.

  * :func:`write_json` renders to a sibling temp file and ``os.replace()``s it
    into position, so a crashed or interrupted run leaves the previous archive
    intact rather than a truncated one. It also pins LF newlines and UTF-8 so
    the file is byte-identical whether it was written on the Windows dev box
    or the Linux GitHub Actions runner.

  * :func:`write_json_stable` skips the write entirely when the only thing that
    would change is the ``generated_at`` clock. The scheduled refresh runs every
    six hours and commits whatever ``git diff`` reports; without this, a quiet
    week would still produce 28 empty commits.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Merge policy
# ---------------------------------------------------------------------------

# Lifecycle fields: the operation itself changed in the real world. A spray
# announced as "scheduled" in the Current section becomes "COMPLETE" in the
# Past section a few days later, and a "POSTPONED" one reappears under a new
# date. These track that lifecycle.
MUTABLE_FIELDS = ("status", "status_text", "section")

# Derived fields: not facts that changed, but our *reading* of source text that
# has not changed. When the parser improves, a re-observation must be able to
# correct what a worse parser stored -- otherwise a bug is welded into the
# archive forever, since the district's page is the only place the original
# text lives and it rolls off after ~2 months.
#
# This is what let the 2020 product bug be repaired in place: six years of rows
# had sentence fragments ("July 12", "depending on weather") stored as
# pesticide names, and the snapshots they came from are cached locally, so a
# re-parse fixes them without refetching anything.
#
# SAFETY RULE (enforced in merge_operations): a derived field is overwritten
# only when the incoming observation carries a non-empty value. A parser
# regression can therefore fail to improve a record, but can never blank one.
DERIVED_FIELDS = (
    "method", "target", "products", "products_raw", "period",
    "time_start", "time_end", "area_name", "boundary_text", "map_url",
)

# Provenance markers used in the Operation ``source`` field.
LIVE_SOURCE = "live"
WAYBACK_PREFIX = "wayback:"


def observation_rank(source: str | None) -> tuple[int, str]:
    """
    Return a sortable "how recent is this observation?" key for a ``source``.

    Two observations of the same operation can disagree — a Wayback snapshot
    from August 2025 will say a spray is *scheduled* while the live page (or a
    later snapshot) says it is *complete*. The higher-ranked observation wins.

    Ordering, lowest to highest:

      * unknown / empty provenance          -> (0, "")
      * ``wayback:<YYYYMMDDhhmmss>``        -> (1, "<YYYYMMDDhhmmss>")
      * ``live``                            -> (2, "")

    Wayback timestamps are fixed-width ``YYYYMMDDhhmmss``, so comparing them as
    strings is the same as comparing them as instants — no parsing required.

    The live page always outranks every snapshot. A capture is by definition of
    a page state that already happened, and the live page is re-scraped every
    six hours, so anything still listed there gets corrected on the next run
    regardless. This is what stops a ``--backfill`` sweep from rewinding a
    finished operation back to "scheduled".
    """
    s = (source or "").strip()
    if s == LIVE_SOURCE:
        return (2, "")
    if s.startswith(WAYBACK_PREFIX):
        return (1, s[len(WAYBACK_PREFIX):])
    # Unknown provenance sorts below everything, so a record written by some
    # older/other code path is always superseded by a real observation.
    return (0, "")


def merge_operations(
    existing: list[dict], incoming: list[dict]
) -> tuple[list[dict], int]:
    """
    Fold ``incoming`` observations into the ``existing`` archive.

    Returns ``(merged, n_new)`` where ``merged`` is the complete archive sorted
    by ``(date, id)`` and ``n_new`` counts ids that were not previously present.

    Semantics:

      * Append-only. Every id in ``existing`` survives into ``merged``, whether
        or not this run saw it again. This is the whole point of the archive.
      * Dedupe by the ``id`` field (``"<date>|<mid>"``). If a record arrives
        without an id but has both a date and a mid, the id is derived; a record
        with neither is unusable and is dropped.
      * For an id already archived, the fields in :data:`MUTABLE_FIELDS` are
        refreshed from the incoming observation *only if* that observation
        ranks at least as high as the one currently reflected in the record
        (see :func:`observation_rank`). ``source`` is refreshed alongside them,
        because it names the observation those values came from.
      * ``first_seen`` is never overwritten. It records when this project first
        learned the operation existed, which no later observation can change.
      * Equal-ranked observations resolve last-one-wins. That is deliberate:
        within a single page scrape the Current section is parsed before the
        Past section, and an operation that appears in both should end up with
        its final, Past-section status.

    Neither argument is mutated; records are shallow-copied on ingest.
    """
    merged: dict[str, dict] = {}

    for record in existing:
        op = _normalize(record)
        if op is not None:
            merged[op["id"]] = op
    n_before = len(merged)

    for record in incoming:
        op = _normalize(record)
        if op is None:
            continue
        current = merged.get(op["id"])
        if current is None:
            merged[op["id"]] = op
            continue

        # Already archived: this is a re-observation, not a new operation.
        if observation_rank(op.get("source")) >= observation_rank(current.get("source")):
            for field in MUTABLE_FIELDS:
                # Only refresh fields the observation actually carries. A parser
                # that legitimately saw no status word sets status_text to None
                # (key present); a parser that failed to emit the key at all
                # should not be able to blank out what we already know.
                if field in op:
                    current[field] = op[field]
            current["source"] = op.get("source", current.get("source"))

        # Derived fields are refreshed regardless of observation rank -- a
        # better parse of an *older* snapshot is still a better parse. The
        # non-empty guard is what keeps this safe: improvements land, blanks
        # never do.
        for field in DERIVED_FIELDS:
            if op.get(field):
                current[field] = op[field]
        # first_seen deliberately untouched.

    ordered = sorted(merged.values(), key=_sort_key)
    return ordered, len(merged) - n_before


def _normalize(record: Any) -> dict | None:
    """
    Shallow-copy an operation record and guarantee it has an ``id``.

    Returns None for anything that cannot be keyed, so a single malformed row
    from an upstream parser cannot poison the merge.
    """
    if not isinstance(record, dict):
        return None
    op = dict(record)

    op_id = op.get("id")
    if not op_id:
        date, mid = op.get("date"), op.get("mid")
        if not (date and mid):
            return None
        op_id = f"{date}|{mid}"
        op["id"] = op_id

    # Defensive only — parse.py and backfill.py stamp this. A record that
    # reaches here without one is being seen for the first time by definition.
    if not op.get("first_seen"):
        op["first_seen"] = utcnow_iso()
    return op


def _sort_key(op: dict) -> tuple[str, str]:
    """Sort operations by date then id, so git diffs stay small and readable."""
    return (str(op.get("date") or ""), str(op.get("id") or ""))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_operations(path: str | os.PathLike) -> list[dict]:
    """
    Load the committed operation archive.

    A missing file yields ``[]`` (first run). A file that exists but is not
    readable as ``{"operations": [...]}`` raises: the caller must treat that as
    fatal and refuse to write, because overwriting an unreadable archive is
    indistinguishable from deleting it.
    """
    doc = _read_json(Path(path), strict=True)
    if doc is None:
        return []
    if isinstance(doc, list):
        # Tolerate a bare list, in case the file was ever hand-edited down to
        # just the operations array.
        return [r for r in doc if isinstance(r, dict)]
    if not isinstance(doc, dict) or not isinstance(doc.get("operations"), list):
        raise ValueError(
            f"{path} is not a spray archive (expected an object with an "
            f"'operations' list, got {type(doc).__name__}). Refusing to treat "
            f"it as empty."
        )
    return [r for r in doc["operations"] if isinstance(r, dict)]


def load_shapes(path: str | os.PathLike) -> dict:
    """
    Load ``shapes.geojson`` as a ``{mid: Feature}`` mapping.

    This is the shape that ``sjmvcd.shapes.fetch_shapes(mids, existing)``
    expects, and the inverse of :func:`shapes_document`. A missing file yields
    ``{}``; an unparseable one raises, for the same reason as the archive.
    """
    doc = _read_json(Path(path), strict=True)
    if doc is None:
        return {}
    features = doc.get("features") if isinstance(doc, dict) else doc
    if not isinstance(features, list):
        raise ValueError(
            f"{path} is not a FeatureCollection (no 'features' list). "
            f"Refusing to treat it as empty."
        )
    out: dict[str, dict] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        mid = props.get("mid") or feature.get("id")
        if mid:
            out[str(mid)] = feature
    return out


def _read_json(path: Path, *, strict: bool) -> Any | None:
    """
    Parse ``path``, returning None when it does not exist.

    ``strict=True`` lets parse/decode errors propagate — used on the archive
    read paths, where a silent empty result is destructive.
    ``strict=False`` swallows them and returns None — used only by
    :func:`write_json_stable`, where an unreadable existing file simply means
    "yes, this needs rewriting".
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        if strict:
            raise
        return None


# ---------------------------------------------------------------------------
# Cross-process locking
# ---------------------------------------------------------------------------

#: A lock older than this is assumed to belong to a process that was killed.
#: Generous, because a Wayback backfill legitimately runs for many minutes.
LOCK_STALE_SECONDS = 90 * 60


class ArchiveBusy(RuntimeError):
    """Raised when another process is already writing the archive."""


#: Lockfiles this process currently holds. Needed to tell two situations apart
#: that otherwise look identical -- a lock stamped with our own pid because WE
#: are holding it right now (genuinely busy, refuse), versus one stamped with
#: our pid by an earlier, crashed process whose id the OS has since recycled
#: (abandoned, safe to steal). Without this distinction the same-pid case has
#: to guess, and either guess is wrong half the time.
_held_locks: set[Path] = set()
_held_lock_guard = threading.Lock()


@contextlib.contextmanager
def archive_lock(data_dir: Path):
    """
    Hold an exclusive, cross-PROCESS lock on the archive for the duration.

    The in-process ``threading.Lock`` in serve.py cannot see a second copy of
    the application, and the frozen build makes that a realistic case: every
    instance resolves the same archive under %LOCALAPPDATA%, and double-clicking
    the exe twice is ordinary user behaviour when a window takes a moment to
    appear. Two concurrent merges would race on the same files.

    Implemented as an O_EXCL lockfile because it is the one primitive that
    behaves identically on Windows and POSIX without a dependency. A lock whose
    owning pid is gone, or that is older than LOCK_STALE_SECONDS, is treated as
    abandoned and stolen -- otherwise a hard kill would leave the app
    permanently unable to update, with no way for a non-developer to clear it.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".refresh.lock"

    def _stale() -> bool:
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            return True
        if age > LOCK_STALE_SECONDS:
            return True
        try:
            owner = int(lock_path.read_text(encoding="utf-8").split()[0])
        except Exception:
            return True          # unreadable lock tells us nothing; treat as dead
        if owner == os.getpid():
            # Ours only if we are actually holding it. If not, this is a
            # leftover from a dead process whose pid got recycled onto us.
            with _held_lock_guard:
                return lock_path.resolve() not in _held_locks
        return not _pid_alive(owner)

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _stale():
            raise ArchiveBusy(
                "another copy of the app is updating the archive right now"
            ) from None
        lock_path.unlink(missing_ok=True)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:  # lost the race to steal it
            raise ArchiveBusy(
                "another copy of the app is updating the archive right now"
            ) from None

    try:
        os.write(fd, f"{os.getpid()} {utcnow_iso()}\n".encode("utf-8"))
        os.close(fd)
        with _held_lock_guard:
            _held_locks.add(lock_path.resolve())
        yield lock_path
    finally:
        with _held_lock_guard:
            _held_locks.discard(lock_path.resolve())
        lock_path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check; assumes alive when it cannot tell."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        except Exception:
            return True
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_json(path: str | os.PathLike, obj: Any, *, sort_keys: bool = True) -> None:
    """
    Write ``obj`` as pretty JSON: UTF-8, two-space indent, trailing newline.

    Written atomically (temp file + ``os.replace``) so an interrupted run can
    never leave a truncated archive behind, and with explicit LF newlines so
    the Windows dev box and the Linux CI runner produce identical bytes.

    ``ensure_ascii`` is off: the source text carries degree signs, registered
    marks and accented street names, and escaping them makes the committed diff
    unreadable for no benefit.

    ``sort_keys`` defaults on. It applies to the GeoJSON too — member order is
    not significant in GeoJSON, and a fixed order means a feature that came off
    disk and one freshly built in memory serialize identically.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, sort_keys=sort_keys, ensure_ascii=False) + "\n"

    # The temp name carries the pid. A fixed "<name>.tmp" is safe against
    # threads (the refresh lock covers those) but NOT against two processes:
    # the frozen app resolves one archive under %LOCALAPPDATA%, and
    # double-clicking the exe twice is the most predictable thing a
    # non-developer does when a window takes a few seconds to appear. Two
    # instances writing the same "<name>.tmp" can interleave their bytes and
    # then both os.replace it into position, publishing a corrupt archive --
    # which is unrecoverable in-app, because the fetcher then refuses to write
    # over an unreadable file and seeding will not re-copy a file that exists.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except BaseException:
        # Never leave a half-written temp behind for the next run to trip over.
        tmp.unlink(missing_ok=True)
        raise


def write_json_stable(
    path: str | os.PathLike,
    obj: Any,
    *,
    timestamp_key: str = "generated_at",
    sort_keys: bool = True,
) -> bool:
    """
    Write ``obj``, unless the only difference from the file on disk is the clock.

    Returns True if the file was written, False if it was left untouched.

    The refresh job runs every six hours and commits whatever ``git diff``
    reports under ``data/``. Outside spray season most runs find nothing new, so
    a ``generated_at`` that always advanced would produce a stream of commits
    whose entire content is a timestamp. Holding the timestamp still until
    something real changes makes ``generated_at`` mean "as of when this data was
    last different", which is the more useful reading anyway.
    """
    previous = _read_json(Path(path), strict=False)
    if (
        isinstance(previous, dict)
        and isinstance(obj, dict)
        and timestamp_key in previous
        and timestamp_key in obj
    ):
        without_clock_old = {k: v for k, v in previous.items() if k != timestamp_key}
        without_clock_new = {k: v for k, v in obj.items() if k != timestamp_key}
        if without_clock_old == without_clock_new:
            return False
    elif previous is not None and previous == obj:
        # No timestamp field at all (shapes.geojson): plain equality is enough.
        return False

    write_json(path, obj, sort_keys=sort_keys)
    return True


# ---------------------------------------------------------------------------
# Document assembly — the inverse of the loaders above. Kept next to them so
# the read and write shapes cannot drift apart.
# ---------------------------------------------------------------------------

def operations_document(operations: Iterable[dict], generated_at: str) -> dict:
    """Build the ``data/operations.json`` document from merged operations."""
    return {
        "generated_at": generated_at,
        "operations": sorted(operations, key=_sort_key),
    }


def shapes_document(shapes: dict) -> dict:
    """
    Build the ``data/shapes.geojson`` FeatureCollection from a ``{mid: Feature}``
    mapping, ordered by mid so the committed file is stable.
    """
    return {
        "type": "FeatureCollection",
        "features": [shapes[mid] for mid in sorted(shapes)],
    }


def utcnow_iso() -> str:
    """Current UTC instant as ``YYYY-MM-DDTHH:MM:SSZ`` (the contract's format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
