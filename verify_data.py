"""
Integrity checks for the committed spray archive.

Run standalone (`python verify_data.py`) or in CI. Exits non-zero if any
invariant fails, which is what stops a bad parse from being committed.

WHY THIS EXISTS
data/ is not a cache. The district's page retains roughly two months of spray
operations; once an entry rolls off, this archive is the only remaining record
of it and cannot be re-derived from any upstream. A silent parser regression
that emitted 800 malformed rows would therefore be permanent. The scheduled
job already refuses to commit an archive that SHRANK; these checks cover the
failure it cannot see -- an archive that is the right size but wrong.

Checks are grouped and every failure is reported, not just the first, so one
run tells you everything that is broken.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

# Resolve the archive through the same single source of truth the fetcher uses
# (sjmvcd.paths), so this gate always checks the file that was actually
# written. Using __file__ here would, in a frozen build, verify the pristine
# bundled seed instead of the user's real archive -- a check that can only ever
# pass and therefore proves nothing.
#
# In a normal dev checkout paths.data_dir() is <repo>/data, so this is exactly
# the directory the previous `Path(__file__).parent / "data"` produced.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sjmvcd import paths  # noqa: E402

DATA_DIR = paths.data_dir()
OPERATIONS_PATH = DATA_DIR / "operations.json"
SHAPES_PATH = DATA_DIR / "shapes.geojson"

# Closed vocabularies. A value outside these means the parser invented
# something, which is exactly the class of bug that produced "July 12" and
# "depending on weather" as pesticide names.
METHODS = {"ground", "aerial"}
TARGETS = {"adult", "larval"}
STATUSES = {"scheduled", "complete", "postponed", "cancelled"}
SECTIONS = {"current", "past"}
PRODUCTS = {
    "Evergreen 5-25", "DeltaGard", "Dibrom",
    "VectoBac WDG", "Altosid", "Pyronyl 525",
}

REQUIRED_FIELDS = (
    "id", "date", "method", "target", "products", "area_name",
    "mid", "map_url", "status", "section", "source", "first_seen",
)

# San Joaquin County with a generous margin. A coordinate outside this is a
# projection or lon/lat-swap bug, not a real spray zone.
LON_MIN, LON_MAX = -122.0, -120.5
LAT_MIN, LAT_MAX = 37.2, 38.5

# The district's earliest archived posting. Anything before this is a
# misparsed date, not history.
EARLIEST_PLAUSIBLE = date(2015, 1, 1)

# Google My Maps documents that no polygon will ever exist for -- listed
# explicitly so the "every operation has a shape" check stays strict: a NEW
# unresolvable id is a real failure worth investigating, rather than something
# that blends into a tolerated background of missing geometry.
#
# Currently EMPTY, and that is a result rather than an oversight. The one entry
# this ever held (1PG7iZX-..., the 2020 Stockton Brookside alerts) turned out to
# be a mislink rather than a deletion: the District supplied the correct map on
# 2026-07-31 and sjmvcd.parse.MISLINKED_MIDS now redirects it, so every
# operation in the archive resolves to a real polygon. The check below also
# fails if an id listed here starts resolving again, which is what flagged this
# one as fixed.
KNOWN_MISSING_SHAPES: set[str] = set()

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Report:
    """Collects failures so one run surfaces every problem at once."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, message: str) -> bool:
        if not ok:
            self.failures.append(message)
        return ok

    def note(self, message: str) -> None:
        self.notes.append(message)


def verify_operations(ops: list[dict], report: Report) -> None:
    """Field presence, closed vocabularies, id derivation and date sanity."""
    report.check(bool(ops), "operations.json contains no operations")

    ids = Counter(op.get("id") for op in ops)
    dupes = [i for i, n in ids.items() if n > 1]
    report.check(not dupes, f"duplicate operation ids: {dupes[:5]}")

    missing: Counter[str] = Counter()
    bad_id: list[str] = []
    bad_date: list[str] = []
    bad_enum: list[str] = []
    bad_product: Counter[str] = Counter()

    horizon = date.today() + timedelta(days=60)
    for op in ops:
        for field in REQUIRED_FIELDS:
            if op.get(field) in (None, "", []):
                # products may legitimately be empty when a header names no
                # pesticide; everything else in REQUIRED_FIELDS must be set.
                if field != "products":
                    missing[field] += 1

        op_id, op_date, mid = op.get("id"), op.get("date"), op.get("mid")
        if op_id != f"{op_date}|{mid}":
            bad_id.append(str(op_id))

        if not (isinstance(op_date, str) and ISO_DATE_RE.match(op_date)):
            bad_date.append(f"{op_id}: {op_date!r}")
        else:
            try:
                parsed = date.fromisoformat(op_date)
                if not (EARLIEST_PLAUSIBLE <= parsed <= horizon):
                    bad_date.append(f"{op_id}: {op_date} out of plausible range")
            except ValueError:
                bad_date.append(f"{op_id}: {op_date!r} not a real date")

        if op.get("method") not in METHODS:
            bad_enum.append(f"{op_id}: method={op.get('method')!r}")
        if op.get("target") not in TARGETS:
            bad_enum.append(f"{op_id}: target={op.get('target')!r}")
        if op.get("status") not in STATUSES:
            bad_enum.append(f"{op_id}: status={op.get('status')!r}")
        if op.get("section") not in SECTIONS:
            bad_enum.append(f"{op_id}: section={op.get('section')!r}")

        for product in op.get("products") or []:
            if product not in PRODUCTS:
                bad_product[product] += 1

    report.check(not missing, f"operations missing required fields: {dict(missing)}")
    report.check(not bad_id, f"{len(bad_id)} ids do not equal '<date>|<mid>': {bad_id[:3]}")
    report.check(not bad_date, f"{len(bad_date)} malformed/implausible dates: {bad_date[:3]}")
    report.check(not bad_enum, f"{len(bad_enum)} values outside their vocabulary: {bad_enum[:5]}")
    report.check(
        not bad_product,
        f"products outside the canonical set (parser leaked prose?): {dict(bad_product)}",
    )

    dates = sorted(op["date"] for op in ops if isinstance(op.get("date"), str))
    if dates:
        report.note(f"{len(ops)} operations, {len(set(dates))} distinct days, "
                    f"{dates[0]} .. {dates[-1]}")


def verify_shapes(shapes: dict, report: Report) -> set[str]:
    """Geometry validity, ring closure and in-county coordinates."""
    features = shapes.get("features") or []
    report.check(bool(features), "shapes.geojson contains no features")

    seen: set[str] = set()
    unclosed = 0
    out_of_county = 0
    bad_geom: list[str] = []
    too_few_points = 0

    for feature in features:
        props = feature.get("properties") or {}
        mid = props.get("mid")
        if mid:
            seen.add(mid)
        geom = feature.get("geometry") or {}
        gtype = geom.get("type")
        if gtype not in ("Polygon", "MultiPolygon"):
            bad_geom.append(f"{mid}: {gtype!r}")
            continue

        polygons = [geom["coordinates"]] if gtype == "Polygon" else geom["coordinates"]
        for polygon in polygons:
            for ring in polygon:
                # A closed ring repeats its first point, so a real triangle has
                # 4 entries. Fewer than 4 cannot enclose an area.
                if len(ring) < 4:
                    too_few_points += 1
                if ring and ring[0] != ring[-1]:
                    unclosed += 1
                for point in ring:
                    lon, lat = point[0], point[1]
                    if not (LON_MIN <= lon <= LON_MAX and LAT_MIN <= lat <= LAT_MAX):
                        out_of_county += 1
                        break

    report.check(not bad_geom, f"features with non-polygon geometry: {bad_geom[:5]}")
    report.check(unclosed == 0, f"{unclosed} unclosed rings")
    report.check(too_few_points == 0, f"{too_few_points} rings with fewer than 4 points")
    report.check(out_of_county == 0, f"{out_of_county} rings with coordinates outside San Joaquin County")

    duplicate_ids = len(features) - len(seen)
    report.check(duplicate_ids == 0, f"{duplicate_ids} duplicate mids in shapes.geojson")

    areas = [p for p in (f.get("properties", {}).get("area_sq_km") for f in features) if p]
    if areas:
        areas.sort()
        report.note(f"{len(features)} shapes, area_sq_km min={areas[0]:.2f} "
                    f"median={areas[len(areas) // 2]:.2f} max={areas[-1]:.2f}")
    return seen


def verify_join(ops: list[dict], shape_mids: set[str], report: Report) -> None:
    """Every operation must resolve to a polygon, or be a known deletion."""
    op_mids = {op.get("mid") for op in ops if op.get("mid")}
    unresolved = op_mids - shape_mids - KNOWN_MISSING_SHAPES
    report.check(
        not unresolved,
        f"{len(unresolved)} map ids have no shape and are not known-deleted: "
        f"{sorted(unresolved)[:5]}",
    )

    stale = KNOWN_MISSING_SHAPES & shape_mids
    report.check(
        not stale,
        f"KNOWN_MISSING_SHAPES lists ids that now resolve -- remove them: {sorted(stale)}",
    )

    orphans = shape_mids - op_mids
    # Not fatal: a shape with no operation is dead weight, not corruption.
    if orphans:
        report.note(f"{len(orphans)} shapes have no referencing operation")

    expected_missing = op_mids & KNOWN_MISSING_SHAPES
    if expected_missing:
        affected = sum(1 for op in ops if op.get("mid") in expected_missing)
        report.note(f"{affected} operations have no polygon (district deleted "
                    f"{len(expected_missing)} map(s) upstream)")


def main() -> int:
    report = Report()

    # Say which archive was checked only when it is NOT the familiar dev one,
    # so a normal checkout's (and CI's) output stays exactly as it was.
    if paths.is_frozen() or os.environ.get(paths.DATA_DIR_ENV):
        print(f"  checking {DATA_DIR}")

    try:
        ops = json.loads(OPERATIONS_PATH.read_text(encoding="utf-8"))["operations"]
    except Exception as exc:
        print(f"FAIL: cannot read {OPERATIONS_PATH}: {exc}")
        return 1
    try:
        shapes = json.loads(SHAPES_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"FAIL: cannot read {SHAPES_PATH}: {exc}")
        return 1

    verify_operations(ops, report)
    shape_mids = verify_shapes(shapes, report)
    verify_join(ops, shape_mids, report)

    for note in report.notes:
        print(f"  {note}")
    if report.failures:
        print(f"\nFAILED {len(report.failures)} check(s):")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    print("\nAll archive integrity checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
