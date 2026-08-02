"""
Build shareable exports of the spray archive.

  python export_data.py

Writes into data/exports/:

  operations.csv       one row per operation, opens directly in Excel
  operations.geojson   one FEATURE per operation, opens directly in QGIS/ArcGIS

WHY TWO SHAPES OF THE SAME DATA
The archive stores operations and geometry separately, because a zone is sprayed
many times and storing its polygon once per spray would triple the repository
for no gain. That normalisation is right for the archive and wrong for everyone
else: a spreadsheet user wants a table, and a GIS user wants geometry with the
attributes already attached. Both exports denormalise the join back out.

The GeoJSON therefore repeats a zone's polygon on every operation that used it.
That is deliberate. A GIS user filtering to "August 2025, aerial, Dibrom" needs
each matching operation to carry its own geometry; a file with one feature per
zone cannot answer that question without a join the tool may not offer.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sjmvcd import paths  # noqa: E402

DATA_DIR = paths.data_dir()
EXPORT_DIR = DATA_DIR / "exports"

# Region names keyed by the leading capitals of the district's own zone codes.
REGION_NAME = {
    "D": "Delta", "E": "Escalon", "L": "Lodi", "LN": "Linden", "M": "Manteca",
    "MH": "Mountain House", "R": "Ripon", "S": "Stockton", "SC": "South County",
    "T": "Tracy", "TH": "Thornton",
}

# Columns in the order a reader wants them: when and what first, then where,
# then the identifiers that let a row be traced back to the district's alert.
COLUMNS = [
    "date", "method", "target", "products", "period", "time_start", "time_end",
    "status", "status_text", "area_name", "zone_code", "region", "boundary_text",
    "area_sq_km", "centroid_lat", "centroid_lon", "map_url", "map_id",
    "observed_via", "first_seen",
]


def region_of(zone_code: str | None) -> str:
    """Region from the leading letters of a zone code, e.g. SC3 -> South County."""
    if not zone_code:
        return "Unknown"
    m = re.match(r"^[A-Z]+", zone_code)
    return REGION_NAME.get(m.group(0), "Unknown") if m else "Unknown"


def rings_of(geometry: dict) -> list:
    """Every linear ring in a Polygon or MultiPolygon."""
    if not geometry:
        return []
    if geometry["type"] == "Polygon":
        return list(geometry["coordinates"])
    return [ring for poly in geometry["coordinates"] for ring in poly]


def centroid(geometry: dict) -> tuple[float | None, float | None]:
    """
    Area-weighted centroid of the exterior ring, as (lat, lon).

    The shoelace centroid, not the mean of the vertices: a zone traced with many
    points along one edge would pull a vertex mean toward that edge. Falls back
    to the vertex mean for a degenerate ring, which cannot happen in this data
    but would otherwise divide by zero.
    """
    rings = rings_of(geometry)
    if not rings:
        return None, None
    ring = rings[0]
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[i + 1][0], ring[i + 1][1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return round(sum(ys) / len(ys), 6), round(sum(xs) / len(xs), 6)
    a *= 0.5
    return round(cy / (6 * a), 6), round(cx / (6 * a), 6)


def load() -> tuple[list[dict], dict]:
    ops = json.loads((DATA_DIR / "operations.json").read_text(encoding="utf-8"))["operations"]
    shapes = json.loads((DATA_DIR / "shapes.geojson").read_text(encoding="utf-8"))
    by_mid = {f["properties"]["mid"]: f for f in shapes["features"]}
    return ops, by_mid


def row_for(op: dict, feature: dict | None) -> dict:
    props = (feature or {}).get("properties", {}) or {}
    lat, lon = centroid((feature or {}).get("geometry")) if feature else (None, None)
    zone = props.get("zone_code")
    return {
        "date": op["date"],
        "method": op["method"],
        "target": op["target"],
        # Semicolon, not comma: a tank mix inside a comma-separated file is how
        # a column quietly becomes two in a tool that re-splits on commas.
        "products": "; ".join(op.get("products") or []),
        "period": op.get("period") or "",
        "time_start": op.get("time_start") or "",
        "time_end": op.get("time_end") or "",
        "status": op["status"],
        "status_text": op.get("status_text") or "",
        "area_name": op.get("area_name") or "",
        "zone_code": zone or "",
        "region": region_of(zone),
        "boundary_text": op.get("boundary_text") or "",
        "area_sq_km": props.get("area_sq_km") if props.get("area_sq_km") is not None else "",
        "centroid_lat": lat if lat is not None else "",
        "centroid_lon": lon if lon is not None else "",
        "map_url": op.get("map_url") or "",
        "map_id": op["mid"],
        "observed_via": op.get("source") or "",
        "first_seen": op.get("first_seen") or "",
    }


def write_csv(rows: list[dict], path: Path) -> None:
    # utf-8-sig, i.e. with a BOM. Excel on Windows assumes the system codepage
    # for a plain UTF-8 CSV and mangles every non-ASCII character -- the degree
    # signs and the en dashes in the district's own area names. The BOM is what
    # makes a double-click open correctly, which is the whole point of the file.
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)


def write_geojson(ops: list[dict], by_mid: dict, rows_by_id: dict, path: Path) -> int:
    features = []
    lons: list[float] = []
    lats: list[float] = []
    skipped = 0
    for op in ops:
        feature = by_mid.get(op["mid"])
        if not feature or not feature.get("geometry"):
            skipped += 1
            continue
        geom = feature["geometry"]
        for ring in rings_of(geom):
            for pt in ring:
                lons.append(pt[0])
                lats.append(pt[1])
        props = dict(rows_by_id[op["id"]])
        props["operation_id"] = op["id"]
        features.append({"type": "Feature", "id": op["id"],
                         "properties": props, "geometry": geom})

    doc = {
        "type": "FeatureCollection",
        # RFC 7946: coordinates are WGS84 lon/lat and no "crs" member is
        # written. Naming a CRS is what older GeoJSON did and is now invalid;
        # a tool that reads it may reproject data that was never projected.
        "bbox": [min(lons), min(lats), max(lons), max(lats)] if lons else None,
        "features": features,
    }
    if doc["bbox"] is None:
        del doc["bbox"]
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return skipped


def main() -> int:
    ops, by_mid = load()
    ops = sorted(ops, key=lambda o: (o["date"], o.get("area_name") or ""))
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    rows = [row_for(op, by_mid.get(op["mid"])) for op in ops]
    rows_by_id = {op["id"]: row for op, row in zip(ops, rows)}

    csv_path = EXPORT_DIR / "operations.csv"
    geo_path = EXPORT_DIR / "operations.geojson"
    write_csv(rows, csv_path)
    skipped = write_geojson(ops, by_mid, rows_by_id, geo_path)

    dates = [o["date"] for o in ops]
    print(f"operations   : {len(ops)}  ({dates[0]} .. {dates[-1]})")
    print(f"distinct zones: {len({o['mid'] for o in ops})}")
    print(f"{csv_path.name:22s} {csv_path.stat().st_size:>9,d} B  {len(rows)} rows")
    print(f"{geo_path.name:22s} {geo_path.stat().st_size:>9,d} B  "
          f"{len(ops) - skipped} features"
          + (f"  ({skipped} operations had no polygon)" if skipped else ""))
    print(f"\nwritten to {EXPORT_DIR}")
    print(f"generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
