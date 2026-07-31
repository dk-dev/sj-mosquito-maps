"""
Refresh the San Joaquin County spray-map archive. One command does the job:

    python fetch_data.py                    scrape the live page, fetch new shapes
    python fetch_data.py --backfill         also sweep the Wayback Machine
    python fetch_data.py --backfill-limit 5 sweep only the 5 newest captures
    python fetch_data.py --shapes-only      skip scraping; just fill shape gaps
    python fetch_data.py --no-shapes        skip Google; just refresh operations
    python fetch_data.py --dry-run          do everything except write files

Stages, in order:

  1. Scrape https://www.sjmosquito.org/News-Spray-Alerts/Spray-Alerts-Maps
     into Operation records.
  2. Optionally walk Wayback Machine captures of that same page for operations
     the district has already rolled off (it retains roughly two months).
  3. Merge both into data/operations.json -- append-only, never destructive.
  4. Fetch the Google My Maps KML for every mid the archive knows about and
     data/shapes.geojson does not, appending to that FeatureCollection.
  5. Write a run summary to data/manifest.json.

WHY THE FAILURE HANDLING LOOKS PARANOID: data/operations.json is committed and
is NOT regenerable. The district's page is the only publisher, and it forgets.
Every stage is therefore isolated -- a Wayback outage, a Google rate-limit, or
a CMS redesign degrades this run to "no new data" and leaves the committed
archive exactly as it was. The process exits non-zero only on total failure
(nothing scraped AND nothing archived), so a transient upstream blip never
fails the scheduled job or, worse, commits a truncated archive.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

# Force UTF-8 stdout. Area names carry non-ASCII (accented street names, the
# registered mark on product names) and the default Windows console codepage
# raises UnicodeEncodeError on them mid-run.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from sjmvcd import archive, paths

# -----------------------------------------------------------------------------
# Paths and constants
# -----------------------------------------------------------------------------
# Resolved through sjmvcd.paths rather than __file__ so that a frozen build
# writes to a directory that still exists after the process exits. In a normal
# dev checkout paths.data_dir() is <repo>/data, i.e. byte-identical behaviour
# to the previous `Path(__file__).resolve().parent / "data"`.
#
# These are module-level constants because the whole file already reads them
# that way; they are resolved once, at import, which also means an in-process
# caller (serve.py's /refresh) gets a stable answer for the length of the run.
ROOT = paths.bundle_dir()
DATA_DIR = paths.data_dir()
OPERATIONS_PATH = DATA_DIR / "operations.json"
SHAPES_PATH = DATA_DIR / "shapes.geojson"
MANIFEST_PATH = DATA_DIR / "manifest.json"

# Raw Wayback captures land here. Gitignored: regenerable, and each capture is
# a few hundred KB of DNN markup we only need once.
CACHE_DIR = paths.cache_dir()

PAGE_URL = "https://www.sjmosquito.org/News-Spray-Alerts/Spray-Alerts-Maps"


# =============================================================================
# Stage 1 -- the live page
# =============================================================================
def scrape_live() -> tuple[list[dict], dict]:
    """
    Fetch and parse the district's spray-alerts page.

    Imports are local to the stage so that a syntax error or missing dependency
    in one sibling module cannot stop the others from running -- the same reason
    the reference project imports Herbie inside its wind fetcher.
    """
    from sjmvcd import http as sj_http
    from sjmvcd import parse as sj_parse

    html = sj_http.get_text(PAGE_URL)
    operations = sj_parse.parse_operations(html, source="live")
    return operations, {
        "url": PAGE_URL,
        "html_bytes": len(html),
        "operations": len(operations),
        "unique_mids": len({o.get("mid") for o in operations if o.get("mid")}),
    }


# =============================================================================
# Stage 2 -- the Wayback Machine
# =============================================================================
def scrape_wayback(limit: int | None, errors: list[str]) -> tuple[list[dict], dict]:
    """
    Walk archived captures of the same page for operations that have rolled off.

    Captures are enumerated oldest-first so that provenance (``source`` /
    ``first_seen``) ends up pointing at the earliest observation of a record.
    Per-capture failures are collected into ``errors`` by the backfill module
    rather than raised, so one 404 in the middle of a 35-capture sweep does not
    discard the 34 that worked.
    """
    from sjmvcd import backfill as sj_backfill

    snapshots = sj_backfill.list_snapshots(limit)
    total = len(snapshots)
    print(f"  {total} usable captures"
          + (f" ({snapshots[0]} .. {snapshots[-1]})" if snapshots else ""))

    # The callback reports (timestamp, operations parsed from this capture,
    # operations new to this sweep) -- it does not carry a position, so count
    # captures here to give the run log a progress bar.
    seen = 0

    def on_progress(timestamp: str, n_parsed: int, n_new: int) -> None:
        nonlocal seen
        seen += 1
        print(f"    [{seen}/{total}] {timestamp}: {n_parsed} parsed, "
              f"{n_new} new to this sweep", flush=True)

    operations = _call_backfill(sj_backfill, snapshots, errors, on_progress)
    return operations, {
        "snapshots": len(snapshots),
        "snapshot_first": snapshots[0] if snapshots else None,
        "snapshot_last": snapshots[-1] if snapshots else None,
        "operations": len(operations),
        "unique_mids": len({o.get("mid") for o in operations if o.get("mid")}),
    }


def _call_backfill(module, snapshots, errors, on_progress) -> list[dict]:
    """
    Call ``backfill_operations`` with the richest signature it supports.

    The module contract only guarantees ``backfill_operations(snapshots)``. The
    implementation additionally accepts an error sink, a capture cache and a
    progress callback; use them when present and fall back cleanly when not, so
    this orchestrator keeps working against either version.
    """
    try:
        return module.backfill_operations(
            snapshots,
            cache_dir=CACHE_DIR,
            errors=errors,
            per_snapshot=on_progress,
        )
    except TypeError:
        return module.backfill_operations(snapshots)


# =============================================================================
# Stage 4 -- Google My Maps shapes
# =============================================================================
def fetch_missing_shapes(mids: list[str], existing: dict) -> tuple[dict, list[str]]:
    """
    Fetch KML for the given mids and fold it into the existing shape mapping.

    ``fetch_shapes`` may return either only what it fetched or the full merged
    mapping; overlaying it onto ``existing`` is correct either way and cannot
    drop a shape we already had.
    """
    from sjmvcd import shapes as sj_shapes

    fetched, failed = sj_shapes.fetch_shapes(mids, existing)
    merged = {**existing, **(fetched or {})}

    # Re-derive zone_code for shapes we already had. It is computed from the
    # stored doc_name, so improving the code regex must be able to correct
    # archived features -- otherwise the only way to pick up a fix would be to
    # delete shapes.geojson and refetch 300+ maps from Google. Same principle
    # as archive.DERIVED_FIELDS, and the same non-empty guard: a regex that
    # newly fails to match leaves the stored value alone rather than blanking
    # it. Purely local, so it costs nothing on a normal run.
    for feature in merged.values():
        props = feature.get("properties") or {}
        recomputed = sj_shapes._zone_code(props.get("doc_name"))
        if recomputed and recomputed != props.get("zone_code"):
            props["zone_code"] = recomputed

    return merged, list(failed or [])


# =============================================================================
# Driver
# =============================================================================
def run_stage(name: str, fn, errors: list[str]) -> dict:
    """
    Run one stage, converting any failure into a recorded error.

    Returns a result block for the manifest. The stage's own payload is merged
    in on success; on failure the block carries ``status: "error"`` and the
    exception text, and the caller carries on with whatever the other stages
    produced.

    Elapsed time is printed but deliberately NOT returned. The manifest is a
    committed file, and a wall-clock duration changes on every single run --
    it would defeat the "unchanged run, unchanged file" rule and hand the
    scheduled job an empty diff to commit every six hours. Anyone who needs
    the timings has them in the run log right above.
    """
    print(f"\n[{name}] ...")
    t0 = time.time()
    try:
        payload = fn()
        print(f"[{name}] ok in {round(time.time() - t0, 1)}s -> {payload}")
        return {"status": "ok", **payload}
    except Exception as exc:
        message = f"{name}: {exc.__class__.__name__}: {exc}"
        print(f"[{name}] FAILED after {round(time.time() - t0, 1)}s: {message}")
        traceback.print_exc()
        errors.append(message)
        return {"status": "error", "error": message}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fetch_data.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="also sweep the Wayback Machine for operations the live page has "
             "already dropped (slow: one HTTP round trip per capture)",
    )
    parser.add_argument(
        "--backfill-limit", type=int, metavar="N", default=None,
        help="walk at most N of the newest captures. Implies --backfill.",
    )
    parser.add_argument(
        "--shapes-only", action="store_true",
        help="skip scraping entirely and only fill in shapes the archive is "
             "missing -- useful after a Google rate-limit truncated a run",
    )
    parser.add_argument(
        "--no-shapes", action="store_true",
        help="skip the Google My Maps fetch; refresh operations only",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="run every stage but write nothing to data/",
    )
    args = parser.parse_args(argv)

    # A limit is only meaningful for a sweep, and someone who types it plainly
    # wants one. Turn the sweep on rather than silently ignoring the flag.
    if args.backfill_limit is not None and not args.backfill:
        args.backfill = True

    if args.shapes_only and args.no_shapes:
        parser.error("--shapes-only and --no-shapes together leave nothing to do")
    if args.shapes_only and args.backfill:
        parser.error("--shapes-only skips scraping, so --backfill has no effect")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.time()
    now = archive.utcnow_iso()
    errors: list[str] = []
    sources: dict[str, dict] = {}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"sjmvcd spray-map refresh @ {now}")
    # Only announce the layout when it is NOT the familiar dev one, so a normal
    # checkout's run log stays exactly as it was.
    if paths.is_frozen() or os.environ.get(paths.DATA_DIR_ENV):
        print(f"  layout  : {paths.describe()}")
    print(f"  archive : {OPERATIONS_PATH}")
    print(f"  shapes  : {SHAPES_PATH}")
    if args.dry_run:
        print("  DRY RUN -- nothing will be written")

    # -- Load the existing archive first. ------------------------------------
    # If this fails the file exists but is unreadable, and writing over it would
    # be indistinguishable from deleting the only copy of the history. Bail out
    # of every write path but still leave a manifest explaining why.
    archive_readable = True
    existing_ops: list[dict] = []
    try:
        existing_ops = archive.load_operations(OPERATIONS_PATH)
        print(f"  loaded {len(existing_ops)} archived operations")
    except Exception as exc:
        archive_readable = False
        message = f"archive: cannot read {OPERATIONS_PATH.name}: {exc}"
        print(f"  FATAL: {message}")
        print("  refusing to write operations.json -- fix or restore the file "
              "from git before re-running")
        errors.append(message)

    existing_shapes: dict = {}
    shapes_readable = True
    try:
        existing_shapes = archive.load_shapes(SHAPES_PATH)
        print(f"  loaded {len(existing_shapes)} archived shapes")
    except Exception as exc:
        shapes_readable = False
        message = f"archive: cannot read {SHAPES_PATH.name}: {exc}"
        print(f"  FATAL: {message}")
        errors.append(message)

    # -- Stage 1: live page --------------------------------------------------
    live_ops: list[dict] = []
    if args.shapes_only:
        sources["live"] = {"status": "skipped", "reason": "--shapes-only"}
        print("\n[live] skipped (--shapes-only)")
    else:
        # run_stage returns only the manifest block, so each stage hands its
        # actual payload back through its own holder. One holder per stage,
        # never a shared name: a reordering must not be able to make one
        # stage's closure write into another's results.
        live_holder: dict = {}

        def _live():
            ops, meta = scrape_live()
            live_holder["ops"] = ops
            return meta

        sources["live"] = run_stage("live", _live, errors)
        live_ops = live_holder.get("ops", [])

    # -- Stage 2: Wayback ----------------------------------------------------
    wayback_ops: list[dict] = []
    if args.backfill:
        wayback_holder: dict = {}

        def _wayback():
            ops, meta = scrape_wayback(args.backfill_limit, errors)
            wayback_holder["ops"] = ops
            return meta

        sources["wayback"] = run_stage("wayback", _wayback, errors)
        wayback_ops = wayback_holder.get("ops", [])
    else:
        sources["wayback"] = {"status": "skipped", "reason": "no --backfill"}

    # -- Stage 3: merge ------------------------------------------------------
    # Wayback first, live last. Ranking inside merge_operations already makes
    # the live page authoritative regardless of order, but keeping the most
    # recent observation last matches how ties resolve and reads more honestly.
    merged_ops, new_ops = archive.merge_operations(
        existing_ops, wayback_ops + live_ops
    )
    dates = sorted(op["date"] for op in merged_ops if op.get("date"))
    print(f"\n[merge] {len(existing_ops)} archived + "
          f"{len(live_ops)} live + {len(wayback_ops)} wayback "
          f"-> {len(merged_ops)} total ({new_ops} new)")

    operations_written = False
    if archive_readable and not args.dry_run:
        try:
            operations_written = archive.write_json_stable(
                OPERATIONS_PATH,
                archive.operations_document(merged_ops, now),
            )
            print(f"[merge] {OPERATIONS_PATH.name}: "
                  f"{'written' if operations_written else 'unchanged'}")
        except Exception as exc:
            message = f"archive: writing {OPERATIONS_PATH.name} failed: {exc}"
            print(f"[merge] FAILED: {message}")
            errors.append(message)

    # -- Stage 4: shapes -----------------------------------------------------
    merged_shapes = existing_shapes
    failed_mids: list[str] = []
    wanted_mids = sorted({op["mid"] for op in merged_ops if op.get("mid")})
    missing_mids = [m for m in wanted_mids if m not in existing_shapes]

    if args.no_shapes:
        sources["shapes"] = {"status": "skipped", "reason": "--no-shapes",
                             "missing": len(missing_mids)}
        print(f"\n[shapes] skipped (--no-shapes); {len(missing_mids)} mids "
              f"have no geometry")
    elif not shapes_readable:
        sources["shapes"] = {"status": "skipped",
                             "reason": "shapes.geojson unreadable"}
        print("\n[shapes] skipped -- existing shapes.geojson could not be read")
    elif not missing_mids:
        sources["shapes"] = {"status": "ok", "requested": 0, "fetched": 0,
                             "failed_mids": []}
        print(f"\n[shapes] nothing to fetch; all {len(wanted_mids)} mids "
              f"already have geometry")
    else:
        shapes_holder: dict = {}

        def _shapes():
            print(f"  fetching {len(missing_mids)} new shapes "
                  f"of {len(wanted_mids)} known mids")
            got, failed = fetch_missing_shapes(missing_mids, existing_shapes)
            shapes_holder["shapes"] = got
            shapes_holder["failed"] = failed
            return {
                "requested": len(missing_mids),
                "fetched": len(got) - len(existing_shapes),
                "failed": len(failed),
                # Capped: a total Google outage would otherwise dump every mid
                # in the archive into the committed manifest.
                "failed_mids": sorted(failed)[:25],
            }

        sources["shapes"] = run_stage("shapes", _shapes, errors)
        # On failure the holder is empty and merged_shapes stays the *same
        # object* as existing_shapes, which is what suppresses the rewrite
        # below -- a failed fetch must never rewrite shapes.geojson.
        merged_shapes = shapes_holder.get("shapes", existing_shapes)
        failed_mids = shapes_holder.get("failed", [])

    new_shapes = len(merged_shapes) - len(existing_shapes)
    shapes_written = False
    if shapes_readable and not args.dry_run and merged_shapes is not existing_shapes:
        try:
            shapes_written = archive.write_json_stable(
                SHAPES_PATH, archive.shapes_document(merged_shapes)
            )
            print(f"[shapes] {SHAPES_PATH.name}: "
                  f"{'written' if shapes_written else 'unchanged'}")
        except Exception as exc:
            message = f"archive: writing {SHAPES_PATH.name} failed: {exc}"
            print(f"[shapes] FAILED: {message}")
            errors.append(message)

    # -- Stage 5: manifest ---------------------------------------------------
    manifest = {
        "generated_at": now,
        "page_ops": len(live_ops),
        "new_ops": new_ops,
        "total_ops": len(merged_ops),
        "total_shapes": len(merged_shapes),
        "new_shapes": new_shapes,
        "shape_failures": len(failed_mids),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "sources": sources,
        "errors": errors,
        # Whether the counts above actually describe what is on disk. When the
        # existing archive could not be read the merge never landed, so the
        # totals are of a merge that exists only in memory -- writing them
        # unqualified told the UI the archive had shrunk to whatever the live
        # page happened to hold. Consumers must check this before trusting the
        # counts.
        "archive_written": bool(archive_readable and not args.dry_run),
    }

    if not args.dry_run and not archive_readable:
        # The archive was refused, so these totals describe a merge that never
        # reached disk. Overwriting a good manifest with them would make the UI
        # report a shrunken archive that is in fact intact.
        print(f"\n[manifest] {MANIFEST_PATH.name}: NOT written -- the archive "
              f"could not be read, so this run's counts describe nothing on disk")
    elif not args.dry_run:
        try:
            # Same stable-timestamp rule as the archive: a run that changed
            # nothing leaves a byte-identical manifest, so the scheduled job's
            # `git diff data/` check has nothing to commit. The consequence is
            # that on a quiet run the manifest printed below is the in-memory
            # summary while the file on disk keeps its older generated_at --
            # hence the explicit written/unchanged note.
            written = archive.write_json_stable(MANIFEST_PATH, manifest)
            print(f"\n[manifest] {MANIFEST_PATH.name}: "
                  f"{'written' if written else 'unchanged (nothing new this run)'}")
        except Exception as exc:
            print(f"[manifest] FAILED to write: {exc}")

    print("\n=== summary ===")
    print(f"  live page ops : {len(live_ops)}")
    print(f"  new ops       : {new_ops}")
    print(f"  archive total : {len(merged_ops)}"
          + (f"  ({dates[0]} .. {dates[-1]})" if dates else ""))
    print(f"  shapes total  : {len(merged_shapes)}  "
          f"(+{new_shapes} new, {len(failed_mids)} failed)")
    print(f"  errors        : {len(errors)}")
    for message in errors[:10]:
        print(f"    - {message}")
    print(f"  elapsed       : {round(time.time() - started, 1)}s")

    print("\nmanifest:")
    print(json.dumps(manifest, indent=2))

    # Non-zero ONLY on total failure -- nothing on the page and nothing in the
    # archive. A failed scrape on top of a healthy archive is a bad afternoon,
    # not a broken job, and must not fail the workflow.
    if not archive_readable:
        print("\nEXIT 1: the existing archive could not be read; nothing written.")
        return 1
    if not merged_ops:
        print("\nEXIT 1: no operations scraped and no archive on disk.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
