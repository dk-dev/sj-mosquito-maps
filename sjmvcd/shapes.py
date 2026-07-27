"""Fetch Google My Maps KML for a spray-area ``mid`` and convert it to GeoJSON.

San Joaquin County MVCD publishes each spray area as a Google My Maps document.
The page links to the map viewer/editor, but the same ``mid`` also serves a plain
KML rendering::

    https://www.google.com/maps/d/kml?mid=<MID>&forcekml=1

``forcekml=1`` is important: without it Google returns a zipped KMZ. With it we
get ``text/xml`` KML 2.2 that stdlib ``xml.etree.ElementTree`` can parse, so this
module needs no XML dependency beyond the standard library.

Observed shape of the upstream documents (surveyed against 87 real ``mid`` values
drawn from the live page and from Wayback snapshots back to 2021):

* ``<Document>`` carries a ``<name>`` that begins with the district zone code
  (``D4(b)- Brack-Canal Ranch-Terminous``) and usually a ``<description>`` that
  repeats the area name or the boundary text.
* Exactly one ``<Polygon>`` per document -- the spray area itself.
* Frequently one decorative ``<Point>`` placemark (a photo/label pin), and
  occasionally a ``<LineString>`` left over from the author's drafting. Both are
  noise and are discarded.
* Rings arrive already closed, altitudes are always ``0``, and coordinates are
  ``lon,lat,alt`` triples separated by whitespace.

The parser does not *assume* that survey holds forever: multiple polygons are
emitted as a ``MultiPolygon``, ``innerBoundaryIs`` holes are honoured, unclosed
rings are closed, and altitude is stripped whatever its value.

Public API (fixed by the project's module contract)::

    kml_url(mid) -> str
    kml_to_feature(kml_text, mid) -> dict | None
    fetch_shape(mid) -> dict | None
    fetch_shapes(mids, existing) -> tuple[dict, list[str]]
"""

from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Iterable

# ``sjmvcd.http`` is a sibling module owned by another part of the project. It is
# imported defensively: this module's pure-parsing half (kml_url/kml_to_feature)
# must stay usable -- and unit-testable -- even if the HTTP layer is missing or
# broken, because the pipeline's rule is that one broken component may never stop
# the others from producing output.
try:  # pragma: no cover - trivial import plumbing
    from .http import get_text_or_none, polite_sleep
except ImportError:  # pragma: no cover
    try:
        from sjmvcd.http import get_text_or_none, polite_sleep  # type: ignore
    except ImportError:
        get_text_or_none = None  # type: ignore[assignment]
        polite_sleep = None  # type: ignore[assignment]

__all__ = [
    "KML_NAMESPACE",
    "kml_url",
    "kml_to_feature",
    "fetch_shape",
    "fetch_shapes",
]

log = logging.getLogger(__name__)

#: Namespace used by the documents Google serves. Retained as documentation and
#: for callers that want it; the element lookups below deliberately use the
#: ``{*}`` wildcard (ElementTree >= 3.8) so that a namespace-less or
#: differently-versioned KML document still parses instead of silently yielding
#: zero polygons -- a class of bug that is very easy to ship and very hard to
#: notice, since "no polygons" looks like "empty map".
KML_NAMESPACE = "http://www.opengis.net/kml/2.2"

_KML_ENDPOINT = "https://www.google.com/maps/d/kml"

# Mean Earth radius (IUGG arithmetic mean radius R1), in kilometres.
EARTH_RADIUS_KM = 6371.0088

# Plausibility window for San Joaquin County, CA, with generous slack. The county
# proper spans roughly lon -121.6..-121.0 and lat 37.5..38.3; anything outside the
# window below is not a county spray zone and indicates a wrong or recycled mid.
BOUNDS_LON_MIN = -122.0
BOUNDS_LON_MAX = -120.5
BOUNDS_LAT_MIN = 37.2
BOUNDS_LAT_MAX = 38.5

# District zone code at the head of <Document><name>.
#
# Real observed forms, all of which this pattern must handle:
#   'D4(b)- Brack-Canal Ranch-Terminous'   -> D4(b)   parenthesised sub-zone
#   'S55b-  Southwest Stockton- ...'       -> S55b    lowercase sub-zone letter
#   'S9- Portion of North Stockton'        -> S9      plain
#   'T16-Cental Tracy'                     -> T16     no space after the dash
#   'E70 Rural N. Escalon'                 -> E70     space, no dash at all
#   'SC6 Portion of Rural South County'    -> SC6     two-letter district prefix
#   'D56 - Portion of Mandeville Island'   -> D56     space before the dash
#   'E50C-North Escalon'                   -> E50C    uppercase sub-zone letter
#   'TH-2 Portion of Thornton Area'        -> TH-2    hyphen *inside* the code
#   'LN2 - Linden'                         -> LN2
#   'D7-Wright-Shima-Atlas-Rindge'         -> D7      name itself is hyphen-rich
#
# The trailing lookahead is what keeps the match honest: the code has to end at a
# separator, so 'S55b-' yields 'S55b' rather than 'S55' plus a stray 'b'. Requiring
# at least one digit is what stops ordinary titles ('Untitled map', 'Portion of
# North Stockton') from being mistaken for codes -- those return None, as required,
# rather than a guess.
_ZONE_CODE_RE = re.compile(
    r"""^\s*
    (
      [A-Z]{1,3}              # district letter prefix (D, S, T, E, M, R, L, SC, LN, TH)
      -?                      # optional hyphen that belongs to the code itself
      \d{1,3}                 # zone number
      [A-Za-z]?               # optional sub-zone letter
      (?:\([A-Za-z0-9]\))?    # optional parenthesised sub-zone, e.g. (b)
    )
    (?=$|[\s:,.\-_])          # must terminate at a separator, not mid-word
    """,
    re.VERBOSE,
)


# --------------------------------------------------------------------------- #
# URL
# --------------------------------------------------------------------------- #

def kml_url(mid: str) -> str:
    """Return the ``forcekml`` KML endpoint for a Google My Maps ``mid``.

    ``forcekml=1`` forces uncompressed KML; omitting it yields a KMZ zip which
    would need unpacking before it could be parsed.
    """
    return f"{_KML_ENDPOINT}?mid={mid}&forcekml=1"


# --------------------------------------------------------------------------- #
# Coordinate / ring helpers
# --------------------------------------------------------------------------- #

def _parse_coordinates(text: str | None) -> list[list[float]]:
    """Parse a KML ``<coordinates>`` blob into ``[[lon, lat], ...]``.

    KML coordinate tuples are ``lon,lat[,alt]`` separated by whitespace. The
    altitude component is stripped: GeoJSON positions may carry elevation, but
    every altitude Google emits here is ``0`` and a phantom third ordinate only
    bloats the committed file and confuses consumers.

    Whitespace around the commas is normalised first so that a hand-edited
    ``-121.30, 37.77, 0`` parses the same as the canonical compact form.
    """
    if not text:
        return []

    normalised = re.sub(r"\s*,\s*", ",", text.strip())
    coords: list[list[float]] = []
    for token in normalised.split():
        parts = token.split(",")
        if len(parts) < 2:
            # Stray token (trailing comma, formatting artefact). Skip rather than
            # abort: one malformed tuple should not lose an otherwise good ring.
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        coords.append([lon, lat])
    return coords


def _close_ring(ring: list[list[float]]) -> list[list[float]]:
    """Ensure a linear ring's first and last positions are identical.

    GeoJSON (RFC 7946 sec. 3.1.6) requires closed rings. Google already closes
    them, but archived and hand-authored documents cannot be relied on to.
    """
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring = ring + [list(ring[0])]
    return ring


def _planar_signed_area(ring: list[list[float]]) -> float:
    """Shoelace signed area of a ring in squared degrees.

    Only the *sign* is used, to decide winding order. Over a few kilometres at
    latitude 38 the lon/lat plane is more than good enough to tell clockwise from
    counter-clockwise; the actual surface area is computed spherically below.

    Positive means counter-clockwise in the (lon, lat) plane.
    """
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        total += (x2 - x1) * (y2 + y1)
    # Negated so that positive == counter-clockwise for (x=lon, y=lat).
    return -total / 2.0


def _orient(ring: list[list[float]], counter_clockwise: bool) -> list[list[float]]:
    """Return ``ring`` wound the requested way.

    RFC 7946 sec. 3.1.6 says exterior rings SHOULD be counter-clockwise and holes
    clockwise (the right-hand rule). Enforcing it is cheap and makes the committed
    GeoJSON correct for consumers that honour winding, while costing nothing for
    the many that ignore it.
    """
    if not ring:
        return ring
    is_ccw = _planar_signed_area(ring) >= 0.0
    if is_ccw != counter_clockwise:
        return list(reversed(ring))
    return ring


def _spherical_ring_area_sq_km(ring: list[list[float]]) -> float:
    """Absolute area of a closed ring on a sphere, in square kilometres.

    Method
    ------
    The spherical line-integral form of the polygon area (the same identity
    behind Girard's spherical-excess theorem, and the formula used by Google's
    ``SphericalUtil.computeSignedArea``)::

        A = R^2 / 2 * SUM over edges of  (lon2 - lon1) * (2 + sin lat1 + sin lat2)

    with longitudes/latitudes in radians. It is exact for a polygon of great-circle
    edges on a sphere and needs no projection, so it stays accurate for the large
    Delta island zones where a naive flat-Earth shoelace would drift.

    Accuracy
    --------
    The Earth is modelled as a sphere of the IUGG mean radius (6371.0088 km) rather
    than the WGS84 ellipsoid. At latitude ~38 deg the local ellipsoidal radius of
    curvature differs from the mean radius by a few tenths of a percent, so areas
    are good to roughly +/-0.5 percent -- far tighter than the precision of a
    hand-drawn spray boundary, and ample for the "is this 3 km2 or 300 km2" use
    this figure serves. It also assumes the polygon does not cross the
    antimeridian, which no San Joaquin County zone does.
    """
    if len(ring) < 4:  # fewer than 3 distinct vertices plus closure
        return 0.0

    total = 0.0
    for i in range(len(ring) - 1):
        lon1, lat1 = ring[i]
        lon2, lat2 = ring[i + 1]
        total += math.radians(lon2 - lon1) * (
            2.0 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
        )
    return abs(total * EARTH_RADIUS_KM * EARTH_RADIUS_KM / 2.0)


def _polygon_area_sq_km(rings: list[list[list[float]]]) -> float:
    """Area of one GeoJSON polygon: exterior ring minus any holes."""
    if not rings:
        return 0.0
    area = _spherical_ring_area_sq_km(rings[0])
    for hole in rings[1:]:
        area -= _spherical_ring_area_sq_km(hole)
    return max(area, 0.0)


# --------------------------------------------------------------------------- #
# KML -> GeoJSON
# --------------------------------------------------------------------------- #

def _text_of(element: ET.Element | None) -> str | None:
    """Return an element's stripped text, or ``None`` when absent/empty."""
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _zone_code(doc_name: str | None) -> str | None:
    """Extract the leading district zone code from a ``<Document><name>``.

    Returns ``None`` -- deliberately, rather than a guess -- when the name does
    not start with something that looks like a code.
    """
    if not doc_name:
        return None
    match = _ZONE_CODE_RE.match(doc_name)
    return match.group(1) if match else None


def _polygon_rings(polygon: ET.Element) -> list[list[list[float]]]:
    """Extract ``[exterior, hole, hole, ...]`` from one KML ``<Polygon>``.

    KML nests the geometry as
    ``outerBoundaryIs/LinearRing/coordinates`` plus zero or more
    ``innerBoundaryIs/LinearRing/coordinates``. No document observed upstream has
    used a hole so far, but the structure is part of KML and honouring it costs a
    few lines; silently dropping holes would quietly overstate sprayed area.
    """
    rings: list[list[list[float]]] = []

    outer = polygon.find("{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
    exterior = _close_ring(_parse_coordinates(outer.text if outer is not None else None))
    if len(exterior) < 4:
        # A ring needs at least three distinct vertices plus the closing repeat.
        return []
    rings.append(_orient(exterior, counter_clockwise=True))

    for inner in polygon.findall("{*}innerBoundaryIs/{*}LinearRing/{*}coordinates"):
        hole = _close_ring(_parse_coordinates(inner.text))
        if len(hole) >= 4:
            rings.append(_orient(hole, counter_clockwise=False))

    return rings


def kml_to_feature(kml_text: str, mid: str) -> dict | None:
    """Convert a KML document into a single GeoJSON ``Feature``.

    Parameters
    ----------
    kml_text:
        Raw KML markup as served by ``kml_url(mid)``.
    mid:
        The Google My Maps id; becomes ``Feature.id`` and ``properties.mid``.

    Returns
    -------
    dict | None
        A Feature whose geometry is a ``Polygon`` (one spray area, the normal
        case) or a ``MultiPolygon`` (several), with properties ``mid``,
        ``zone_code``, ``doc_name``, ``doc_description``, ``fetched_at`` and
        ``area_sq_km``.

        ``None`` if the payload will not parse as XML or contains no polygon at
        all -- both of which the caller is expected to record as a failure rather
        than treat as an empty map. Decorative ``Point`` and ``LineString``
        placemarks are discarded; a document holding *only* those counts as
        having no polygon.
    """
    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError as exc:
        # Google occasionally answers a bad mid with an HTML error page, which
        # lands here rather than as a non-200 status.
        log.warning("mid %s: KML did not parse as XML (%s)", mid, exc)
        return None

    # Document-level metadata. Scoped to <Document> so that a Placemark's own
    # <name> ("Polygon 2", "Line 1") can never be mistaken for the map title.
    document = root.find(".//{*}Document")
    if document is not None:
        doc_name = _text_of(document.find("{*}name"))
        doc_description = _text_of(document.find("{*}description"))
    else:
        doc_name = _text_of(root.find(".//{*}name"))
        doc_description = _text_of(root.find(".//{*}description"))

    # Every Polygon anywhere in the tree, including any nested inside a
    # <MultiGeometry>. Points and LineStrings are simply never looked at.
    polygons: list[list[list[float]]] = []
    for polygon_el in root.iter():
        if not polygon_el.tag.endswith("}Polygon") and polygon_el.tag != "Polygon":
            continue
        rings = _polygon_rings(polygon_el)
        if rings:
            polygons.append(rings)

    if not polygons:
        log.warning("mid %s: KML contains no usable polygon (name=%r)", mid, doc_name)
        return None

    if len(polygons) == 1:
        geometry = {"type": "Polygon", "coordinates": polygons[0]}
    else:
        geometry = {"type": "MultiPolygon", "coordinates": polygons}

    area_sq_km = round(sum(_polygon_area_sq_km(rings) for rings in polygons), 4)

    return {
        "type": "Feature",
        "id": mid,
        "properties": {
            "mid": mid,
            "zone_code": _zone_code(doc_name),
            "doc_name": doc_name,
            "doc_description": doc_description,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "area_sq_km": area_sq_km,
        },
        "geometry": geometry,
    }


# --------------------------------------------------------------------------- #
# Bounds sanity check
# --------------------------------------------------------------------------- #

def _iter_positions(geometry: dict) -> Iterable[list[float]]:
    """Yield every ``[lon, lat]`` position in a Polygon/MultiPolygon geometry."""
    if geometry.get("type") == "Polygon":
        polygons = [geometry.get("coordinates") or []]
    else:
        polygons = geometry.get("coordinates") or []
    for rings in polygons:
        for ring in rings:
            for position in ring:
                yield position


def _out_of_bounds(feature: dict) -> str | None:
    """Return a description of the bounds violation, or ``None`` when sane.

    A mid can be recycled by the district or mistyped in the CMS, in which case
    the KML resolves fine but describes somewhere that is not San Joaquin County.
    That must surface as a failure rather than land in the archive unnoticed.
    """
    lons: list[float] = []
    lats: list[float] = []
    for lon, lat in _iter_positions(feature.get("geometry") or {}):
        lons.append(lon)
        lats.append(lat)
    if not lons:
        return "geometry has no positions"

    lon_min, lon_max = min(lons), max(lons)
    lat_min, lat_max = min(lats), max(lats)
    if (
        lon_min < BOUNDS_LON_MIN
        or lon_max > BOUNDS_LON_MAX
        or lat_min < BOUNDS_LAT_MIN
        or lat_max > BOUNDS_LAT_MAX
    ):
        return (
            f"bbox lon {lon_min:.4f}..{lon_max:.4f}, lat {lat_min:.4f}..{lat_max:.4f} "
            f"falls outside San Joaquin County "
            f"(lon {BOUNDS_LON_MIN}..{BOUNDS_LON_MAX}, lat {BOUNDS_LAT_MIN}..{BOUNDS_LAT_MAX})"
        )
    return None


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_shape(mid: str) -> dict | None:
    """Fetch one ``mid`` and return its GeoJSON Feature, or ``None`` on failure.

    Never raises: a dead mid, a network problem, a redirect to an HTML error page
    or an unparseable document all come back as ``None`` with a logged reason, so
    that a batch can continue.
    """
    if get_text_or_none is None:
        # sjmvcd.http is unavailable. Log loudly and degrade instead of raising,
        # so the rest of the pipeline can still emit its own outputs.
        log.error("mid %s: cannot fetch, sjmvcd.http could not be imported", mid)
        return None

    url = kml_url(mid)
    try:
        kml_text = get_text_or_none(url)
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all, see docstring
        log.warning("mid %s: fetch raised %s: %s", mid, type(exc).__name__, exc)
        return None

    if not kml_text:
        log.warning("mid %s: no KML returned from %s", mid, url)
        return None

    try:
        return kml_to_feature(kml_text, mid)
    except Exception as exc:  # noqa: BLE001 - a malformed doc must not kill a run
        log.warning("mid %s: KML conversion raised %s: %s", mid, type(exc).__name__, exc)
        return None


def fetch_shapes(mids, existing: dict) -> tuple[dict, list[str]]:
    """Fetch every ``mid`` not already present in ``existing``.

    Parameters
    ----------
    mids:
        Iterable of Google My Maps ids. Duplicates are collapsed and first-seen
        order is preserved.
    existing:
        Already-known ``mid -> Feature`` mapping, normally the contents of
        ``data/shapes.geojson``. Shapes are immutable once captured -- a spray
        boundary published under a given mid does not change -- so anything
        already present is skipped without a request. This is what keeps the
        nightly job to a handful of calls instead of a hundred-plus.

    Returns
    -------
    (shapes, failed_mids)
        ``shapes`` is the **merged** mapping: everything from ``existing`` plus
        every Feature newly fetched. ``existing`` itself is never mutated, so the
        caller can compute ``len(shapes) - len(existing)`` for the new-shape
        count and write ``shapes`` straight out.

        ``failed_mids`` lists the ids that could not be turned into a usable
        Feature: fetch failures, documents with no polygon, and documents whose
        coordinates fall outside San Joaquin County. Out-of-bounds features are
        excluded from ``shapes`` rather than silently accepted; the specific
        reason for each failure is written to this module's logger.
    """
    merged: dict = dict(existing or {})
    failed: list[str] = []

    # Collapse duplicates while preserving order -- the source page lists the same
    # mid under both the current and past sections when an operation is postponed.
    ordered: list[str] = []
    seen: set[str] = set()
    for mid in mids or ():
        if mid and mid not in seen:
            seen.add(mid)
            ordered.append(mid)

    todo = [mid for mid in ordered if mid not in merged]
    log.info(
        "shapes: %d requested, %d already cached, %d to fetch",
        len(ordered), len(ordered) - len(todo), len(todo),
    )

    for index, mid in enumerate(todo):
        # Space out requests. The sleep goes before every call after the first so
        # a single-shape run pays no penalty.
        if index and polite_sleep is not None:
            try:
                polite_sleep()
            except Exception:  # noqa: BLE001 - a broken sleep must not stop work
                pass

        try:
            feature = fetch_shape(mid)
        except Exception as exc:  # noqa: BLE001 - belt and braces around fetch_shape
            log.warning("mid %s: unexpected error %s: %s", mid, type(exc).__name__, exc)
            feature = None

        if feature is None:
            failed.append(mid)
            continue

        problem = _out_of_bounds(feature)
        if problem:
            log.warning("mid %s: rejected, %s", mid, problem)
            failed.append(mid)
            continue

        merged[mid] = feature

    log.info("shapes: %d total, %d failed", len(merged), len(failed))
    return merged, failed
