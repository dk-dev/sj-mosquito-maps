"""sjmvcd -- San Joaquin County Mosquito & Vector Control District spray-alert archiver.

This package scrapes the district's public "Spray Alerts Maps" page
(https://www.sjmosquito.org/News-Spray-Alerts/Spray-Alerts-Maps), extracts one
record per sprayed *area* per *operation*, fetches the matching Google My Maps
polygon for each area, and maintains an append-only historical archive under
``data/``.  The live page only retains roughly two months of history, so the
Wayback Machine is used to backfill older operations; Google still serves the
KML geometry for archive-only map ids, which is what makes a full-year
timelapse possible.

Module map
----------
``sjmvcd.http``
    Polite HTTP helpers (descriptive User-Agent, retry with exponential
    backoff, inter-request throttling).
``sjmvcd.parse``
    HTML -> Operation dicts.  Pure functions; no network access.
``sjmvcd.shapes``
    Map id -> GeoJSON Feature via the Google My Maps KML endpoint.
``sjmvcd.backfill``
    Wayback Machine CDX enumeration and snapshot parsing.
``sjmvcd.archive``
    Load / merge / write the JSON artefacts in ``data/``.

Design rule that applies to every module: *failure isolation*.  One broken
upstream (the district site, Google, or the Wayback Machine) must never prevent
the others from producing output.  Helpers therefore prefer returning ``None``
and recording an error string over raising.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
