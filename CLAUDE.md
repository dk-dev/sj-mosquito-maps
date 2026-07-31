# CLAUDE.md — sj-mosquito-maps

Timelapse of San Joaquin County mosquito spray operations. Python fetcher +
single-file frontend; no build step.

## Build / install

```bash
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt        # Windows
source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux
```

Two pure-Python deps (`requests`, `beautifulsoup4`). Nothing compiles; a clean
install takes seconds.

## Run

```bash
python app.py                   # native window (or browser) — the normal way to view it
python app.py --refresh         # re-scrape first, then open
python fetch_data.py            # scrape + shapes (~15 s warm, ~3 min cold)
python fetch_data.py --backfill # also sweep the Wayback Machine
python verify_data.py           # archive integrity checks
python serve.py                 # port 8000, also serves POST /refresh
# open http://localhost:8000/
```

On Windows, `view-map.cmd` is double-clickable and wraps `app.py`.

## Toolchain

- Python 3.14 (3.11+ works)
- Frontend is Leaflet 1.9.4 from CDN, no npm, no bundler
- No test framework: `verify_data.py` is the test suite and runs in CI

## File layout

```
app.py                  Desktop launcher — native window via pywebview
view-map.cmd            Double-clickable Windows wrapper around app.py
build-exe.cmd           One-step build -> dist/sj-mosquito-maps.exe
sj-mosquito-maps.spec   PyInstaller build definition (committed, not generated)
fetch_data.py           CLI entry point; runs the stages, writes the archive
verify_data.py          Archive integrity checks (also a CI gate)
serve.py                Static server + POST /refresh (fetch runs IN-PROCESS)
index.html              Entire frontend, single file
vendor/                 Leaflet 1.9.4, vendored — no CDN at runtime
sjmvcd/paths.py         The two roots: read-only bundle vs writable archive
sjmvcd/http.py          Polite session: UA, retry/backoff
sjmvcd/parse.py         Spray-alerts HTML -> operation records
sjmvcd/shapes.py        Google My Maps KML -> GeoJSON
sjmvcd/backfill.py      Wayback CDX -> historical operations
sjmvcd/archive.py       Append-only merge policy
data/                   COMMITTED archive (see below)
data/.cache/            Raw archived HTML (gitignored, regenerable)
```

## Conventions

- **`data/` is committed, and that is the whole point.** This is the opposite
  of the sibling `ca-grid-weather-map`, where `data/` is a gitignored cache.
  The district's page retains only ~2 months of operations; once an entry
  rolls off, this archive is the only remaining record of it and **cannot be
  re-derived from any upstream**. Treat it as primary data, never as a cache.
- **Never let a run shrink the archive.** The scheduled job fails rather than
  committing a smaller `operations.json`. If you change the parser, run
  `verify_data.py` and check the operation count before committing.
- **Two kinds of field, two update rules** (`sjmvcd/archive.py`):
  `MUTABLE_FIELDS` are real lifecycle changes (a scheduled spray becomes
  complete). `DERIVED_FIELDS` are our *reading* of source text that has not
  changed, so a better parser is allowed to correct them on re-observation —
  but only when the incoming value is non-empty, so a regression can fail to
  improve a record and never blank one. Without this, a parser bug is welded
  into the archive permanently.
- **Two roots, and they only coincide in dev** (`sjmvcd/paths.py`). The
  read-only bundle holds `index.html` and `vendor/`; the writable archive holds
  `operations.json` and friends. In a checkout the second nests inside the
  first, which is why one root was enough for years. In the frozen exe the
  bundle is a temp directory **deleted on exit** and the archive lives in
  `%LOCALAPPDATA%\sj-mosquito-maps\data`. Anything resolving data through
  `__file__` is a bug that only shows up after shipping.
- **Nothing may subprocess `sys.executable`.** In the frozen app that is the
  GUI, so spawning it relaunches the window instead of running a scrape, and
  `fetch_data.py` is not on disk to spawn anyway. `/refresh` imports and calls
  the fetcher.
- **Fetchers are failure-isolated.** Each stage records its own error into
  `data/manifest.json` and the run continues. A flaky upstream must never
  cost us the other stages, and must never truncate the archive.
- **Frontend is single-file.** `index.html` carries all the JS/CSS. No build
  step, no bundler, no npm. Keep it that way.
- **Be polite upstream.** Descriptive User-Agent, ~0.4 s between requests,
  retry with backoff. The Wayback CDX endpoint 503s under load — always
  HTTPS, always retry.

## Pitfalls

- **Half of every calendar year is empty.** Jan–Apr and Dec have zero
  operations because the district does not spray in winter. That is real
  absence, not missing data — never render it as a gap or "no data" error.
- **`new Date("2026-07-24")` parses as UTC midnight** and renders as the
  previous day in US timezones. Date handling in `index.html` must construct
  local dates explicitly.
- **A 404ing map id may be a MISLINK, not a deletion.** The district's alert
  for the 2020 Stockton Brookside sprays pointed at
  `1PG7iZX-Eb0uV_kH6VfCE37SqwMoGj00c`, which 404s everywhere. Asking the
  District produced the real map (zone S23) — the alert itself had the wrong
  link. Corrections live in `sjmvcd/parse.py::MISLINKED_MIDS` and are applied
  at PARSE time, not to the stored rows: every Wayback capture still contains
  the wrong link, so a fix applied only to the archive would be undone by the
  next `--backfill`. `verify_data.py::KNOWN_MISSING_SHAPES` is now empty, and
  it fails the build if an id listed there starts resolving again.
- **A map id is not unique per operation.** The same zone is sprayed many
  times, so `mid` repeats across dates. The operation key is `date|mid`.
- **The district's own product spelling drifts** ("Evergreen 5-25" vs
  "Evergreen 525" vs "EverGreen ULV 5-25 Air"). `parse.py` canonicalises onto
  a closed 6-product vocabulary; `verify_data.py` fails the build if anything
  outside it appears, because prose leaking into the product list is exactly
  how the original parser bug showed up.
- **The GitHub Action only runs once this repo has a remote.** It is written
  and validated but has never executed; there is no remote by default.
