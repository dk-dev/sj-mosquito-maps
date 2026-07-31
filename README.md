# San Joaquin Mosquito Spray Timelapse

Interactive timelapse of every mosquito spray operation the San Joaquin County
Mosquito & Vector Control District has published — **823 operations across
2020–2026**, animated day by day over a full calendar year and looping.

The district publishes each spray zone as a Google My Maps link on its
[Spray Alerts & Maps](https://www.sjmosquito.org/News-Spray-Alerts/Spray-Alerts-Maps)
page. This project turns those scattered links into a single archived,
searchable, animated map.

```
[ 823 operations ] x [ 311 spray zones ] x [ 7 years ] x [ 6 pesticides ]
        animated over the calendar year, looping, filterable
```

Built to answer: *where does the county actually spray, with what, and when?*

## The interesting problem

**The district's page only retains about two months of operations.** Once an
entry rolls off, it is gone from the web.

Two facts make a multi-year archive possible anyway:

1. The Wayback Machine has snapshots of that page going back years, and each
   snapshot contains its own rolling ~2-month archive.
2. **Google My Maps documents outlive the page that linked them.** A map id
   scraped from a 2020 snapshot still returns KML today.

So the Wayback Machine supplies the `(date, area, map-id)` tuples and Google
still supplies the geometry. That is where 745 of the 823 operations come from
— 19 archived snapshots — and it is why the timelapse has years of history at
launch rather than after a year of scraping.

Everything scraped is committed to `data/`. That directory is **not a cache**:
once the district's page drops an entry, this archive is the only remaining
record of it and cannot be re-derived.

## Features

- **Year-long timelapse** — scrub or play Jan 1 → Dec 31 of any year (2020–2026)
  or the whole 7-year span, looping continuously
- **Trail decay** — each spray stays visible for an adjustable window (1–30
  days) and fades with age, so the animation shows accumulation instead of
  flicker. Plus a cumulative mode
- **Skip empty stretches** — roughly half of every calendar year has zero
  operations because the district does not spray in winter. Playback can skip
  the dead runs while the scrubber and calendar keep showing the true full
  year, so real seasonality is never mistaken for missing data
- **Calendar heat strip** — every day of the period as a cell colored by
  operation count; click (or arrow-key) to jump. Seasonality at a glance
- **Color by** application method, target stage, or pesticide
- **Five basemaps** — dark, light, terrain light, terrain dark and satellite,
  remembered between launches. No free provider hosts a dark terrain map, so
  that one is composited in the browser from CARTO Dark Matter with Esri's
  World Hillshade multiplied over it. Every layer of every stack carries its
  own provider credit, taken verbatim from that service's own metadata, and
  the polygon styling is re-tuned per background: each zone outline is drawn
  twice — an opaque white or black casing under the coloured line — over a
  tuned per-basemap tint, so the outline clears WCAG's 3:1 on every measured
  pixel of all five backgrounds and the age fade survives the move off black
- **Filters** — method, target stage, status, pesticide, and all 12 district
  regions; filters drive the map, the calendar strip and the stats together
- **Honest statistics** — distinguishes *treatments* from *distinct zones*, and
  says plainly that neither area figure is a treated footprint (the district's
  zones overlap, so both add the same ground more than once)
- **Click any zone** for area name, date, method, pesticides, boundary
  description, status, district zone code, area in km², and a link to the
  district's original map
- **Auto-refreshing** — a scheduled GitHub Action rescans the page, pulls
  shapes for any map it has not seen, and rebuilds the timeline

## Stack

| Layer      | Tech                                                     |
|------------|----------------------------------------------------------|
| Data fetch | Python 3.14, `requests`, `beautifulsoup4`                |
| Shapes     | Google My Maps KML export → GeoJSON (stdlib `xml.etree`) |
| Backfill   | Wayback Machine CDX API                                  |
| Server     | `http.server` subclass with a `POST /refresh` endpoint   |
| Frontend   | Leaflet 1.9.4, vendored, single file, no build step      |
| Tiles      | CARTO (OpenStreetMap) + Esri ArcGIS Online — see below   |

No npm, no bundler, no build step. Two pure-Python dependencies; nothing
compiles.

## Quick start

Prereqs: **Python 3.14** (3.11+ works), Windows / macOS / Linux.

```bash
git clone <this repo>
cd sj-mosquito-maps

py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt        # Windows
source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

python app.py
```

`data/` is committed, so the map works immediately after cloning — no fetch
required.

## Viewing it

There is no hosted demo: the repo is private, and GitHub Pages does not serve
private repositories without a paid plan. Everything below runs locally.

| | |
|---|---|
| `python app.py` | Opens the map in a native window. **The normal way to use it.** |
| `view-map.cmd` | Same thing, double-clickable on Windows. No terminal needed. |
| `python app.py --refresh` | Re-scrapes the district's page, then opens |
| `python app.py --browser` | Skips the native window, uses your default browser |
| `python serve.py` | Just the server on port 8000, if you want a plain tab |

### As a Windows application

```bash
build-exe.cmd
```

Produces `dist\sj-mosquito-maps.exe` (~14 MB) — a single file you can hand to
someone who has no Python. It carries the whole archive and a vendored copy of
Leaflet, so it opens and draws the spray zones with no network at all; only the
basemap tiles need internet. **Update maps** in the app re-scrapes the district's
page and rewrites the archive in place.

The exe keeps its archive in `%LOCALAPPDATA%\sj-mosquito-maps\data`, not beside
the executable — a frozen app's own directory is either a temp folder that is
deleted on exit or a Program Files path it cannot write to. On first run it
copies the archive that shipped inside it to that location, and it only ever
fills in files that are **missing**, so reinstalling or rebuilding can never roll
a months-newer archive back to the build-time snapshot.

The native window needs one optional extra:

```bash
pip install -r requirements-desktop.txt
```

Without it everything still works — `app.py` detects that pywebview is missing
and falls back to your default browser, so the double-click path works on a
bare clone. The window uses the OS's own webview (Edge WebView2 on Windows,
WebKit on macOS, Qt on Linux), so there is no Chromium bundle to install.

`app.py` picks a free port rather than a fixed one, so it never collides with
whatever else is running.

To refresh it yourself:

```bash
python fetch_data.py              # rescan the live page (~15 s)
python fetch_data.py --backfill   # also sweep the Wayback Machine
python verify_data.py             # archive integrity checks
```

Or hit the refresh button in the UI, which does the same thing server-side.

## How it works

```
sjmvcd/parse.py     spray-alerts HTML  ->  operation records
sjmvcd/shapes.py    maps/d/kml?mid=..  ->  GeoJSON polygons
sjmvcd/backfill.py  Wayback CDX        ->  historical operations
sjmvcd/archive.py   append-only merge  ->  data/operations.json
```

Each stage is failure-isolated: a stage that fails records its error into
`data/manifest.json` and the run continues. A flaky upstream never costs the
other stages, and never truncates the archive.

The merge distinguishes two kinds of field:

- **Lifecycle fields** (`status`, `section`) — the operation genuinely changed;
  a scheduled spray becomes complete.
- **Derived fields** (products, times, area names…) — our *reading* of source
  text that has not changed. A better parser is allowed to correct these on
  re-observation, but only when the incoming value is non-empty, so a
  regression can fail to improve a record and can never blank one.

That distinction is load-bearing. Without it a parser bug is welded into the
archive permanently, because the original text only exists on a page that rolls
over every two months. It is what allowed a real bug — six years of rows where
sentence fragments like `"depending on weather"` had been stored as pesticide
names — to be repaired in place.

## File layout

```
app.py                Desktop launcher — native window, falls back to browser
view-map.cmd          Double-clickable Windows wrapper around app.py
build-exe.cmd         One-step Windows build -> dist/sj-mosquito-maps.exe
sj-mosquito-maps.spec PyInstaller build definition (committed, hand-written)
fetch_data.py         CLI entry point; runs the stages, writes the archive
verify_data.py        Archive integrity checks (also a CI gate)
serve.py              Static server + POST /refresh (runs the fetch in-process)
index.html            The entire frontend. Single file, no build step
vendor/               Leaflet 1.9.4, vendored so the app needs no CDN
sjmvcd/paths.py       Resolves the two roots: read-only bundle vs writable archive
sjmvcd/               Fetch/parse/merge package
data/                 COMMITTED archive:
  operations.json       823 operations
  shapes.geojson        310 polygons, keyed by Google map id
  manifest.json         run summary for the banner
  .cache/               raw archived HTML (gitignored, regenerable)
```

## Automation

`.github/workflows/refresh-data.yml` runs every 6 hours: it rescans the page,
fetches shapes for any map id it has not seen before, rebuilds the timeline,
and commits `data/` if anything changed. A weekly run also sweeps the Wayback
Machine to self-heal any gap left by a paused or broken scheduler.

Because the archive is irreplaceable, the job refuses to damage it:

- it **fails rather than commits** if `operations.json` came back smaller than
  it went in, and
- it runs `verify_data.py`, which checks closed vocabularies, id derivation,
  date plausibility, ring closure, in-county coordinates, and that every
  operation still resolves to a polygon.

Those checks were validated against nine deliberately corrupted archives —
leaked prose in the product list, swapped lon/lat, an unclosed ring, a
duplicated id, a blanked required field. All nine were caught.

> The workflow has been **verified green end-to-end** on a GitHub-hosted runner
> (Python 3.14, archive guard and integrity gate both passing, and the
> `GITHUB_TOKEN` correctly elevated to `Contents: write` despite a repository
> default of `read`). That verification ran against a remote that has since
> been deleted, so this repo currently has **no remote** and nothing is
> scheduled. Push it to GitHub and the schedule starts on its own.
>
> Until then, `python fetch_data.py` — or `python app.py --refresh` — is the
> way to bring the archive up to date.

## Data sources

| Layer            | Source                                                    |
|------------------|-----------------------------------------------------------|
| Spray operations | [SJCMVCD Spray Alerts & Maps](https://www.sjmosquito.org/News-Spray-Alerts/Spray-Alerts-Maps) |
| Spray zone shapes| Google My Maps KML export (`maps/d/kml?mid=…`)            |
| Historical pages | [Wayback Machine](https://web.archive.org/) CDX API       |
| Basemaps         | CARTO and Esri ArcGIS Online (table below)                  |

Spray zone geometry and operation details are published by the San Joaquin
County Mosquito & Vector Control District. The scrapers identify themselves by
User-Agent and rate-limit themselves.

### Basemap tile services and their credits

Five basemaps, eight tile services between them. Attribution is bound to each
*layer*, not to each basemap, so a composite cannot silently drop a credit when
its stack is reordered — Leaflet unions and de-duplicates whatever is on the
map. Every string is that service's own `copyrightText`, copied verbatim, with
`(c)` rendered as `©` and the word OpenStreetMap linked to
`openstreetmap.org/copyright`.

| Basemap          | Tile layers                                                                                 | Credited to                                              |
|------------------|---------------------------------------------------------------------------------------------|----------------------------------------------------------|
| Dark             | CARTO `dark_all`                                                                              | © OpenStreetMap contributors, © CARTO                    |
| Light            | CARTO `light_all`                                                                             | © OpenStreetMap contributors, © CARTO                    |
| Terrain (light)  | Esri `World_Topo_Map`                                                                         | Esri and its data providers, incl. © OpenStreetMap contributors |
| Terrain (dark)   | CARTO `dark_nolabels` + Esri `Elevation/World_Hillshade` (multiplied) + CARTO `dark_only_labels` | © OpenStreetMap contributors, © CARTO; and Esri and its data providers |
| Satellite        | Esri `World_Imagery`                                                                          | Esri and its data providers (Vantor, Earthstar Geographics, …) |

Two conditions worth stating plainly. **OpenStreetMap**: Esri's World Topo Map
and CARTO's basemaps are both rendered from OSM data, so the OSM credit is a
licence condition under the ODbL, not a courtesy, and the link to the licence
is part of it. **Esri**: the Terms of Use summary requires attribution to
"Esri *and its data providers*", so the provider list is part of the condition
and not decoration — which is why the lists are copied rather than curated.
Esri's terms also forbid harvesting, redistributing or self-hosting their
tiles: Leaflet is vendored under `vendor/`, Esri tiles are not, and nothing in
the packaged `.exe` caches them.

## Limitations (honest list)

- **Every operation now resolves to a polygon.** One map id (the 2020 Stockton
  Brookside alerts) 404s on every Google endpoint. Asking the District
  established that the *alert* was mislinked rather than the map deleted, and
  they supplied the correct one — zone S23, whose published description matches
  the alert's boundary text word for word. The correction is applied when the
  page is parsed, so re-reading the archived captures that contain the bad link
  cannot reintroduce it. A *new* unresolvable id still fails the build.
- **Coverage is uneven before 2025.** History depends on how often the Wayback
  Machine happened to crawl the page. 2022 has 58 operations and 2025 has 194;
  that gap is snapshot luck, not a change in spraying. Any year-over-year
  comparison is unsound.
- **Winter is genuinely empty, not missing.** Jan–Apr and Dec have zero
  operations across all seven years. The district does not spray then.
- **Announced, not verified.** Every record is what the district *published*
  ahead of an operation. Sprays are cancelled for weather (14 are marked so);
  a completed status is the district's own, not independent confirmation.
- **Zone polygons are the district's own drawings**, at whatever precision
  they chose in Google My Maps. They are not parcel-accurate.
- **Spray zones overlap, so no area figure here is a footprint.** 334 zone
  pairs sit more than 90% inside another, and four pairs are byte-identical
  polygons published under different map ids. Summing zone areas for the whole
  archive gives ~2,498 km², where the true union is closer to **918 km²**. The
  UI labels its figures for what they actually compute ("Zone areas summed",
  "Treatment-area sum"); computing a real union would need a polygon-clipping
  library, which this project deliberately does not carry.
- **Colour by product shows one product per operation.** Most ground
  operations apply two pesticides as a tank mix. The map picks one by a fixed
  precedence so a mix always draws the same colour; the popup and the zone list
  name every product applied. (Choosing by the district's own word order, as an
  earlier version did, made the animation flip colour across 2022–23 purely
  because the wording of the alerts changed.)
- **The dataset is not a health or exposure record.** It shows where the
  district said it would spray and when. It says nothing about drift,
  deposition, or actual exposure.

## Accessibility

Built to WCAG 2.1 AA targets:

- Semantic landmarks, skip link, visible `:focus-visible` rings
- `prefers-reduced-motion` suppresses autoplay; the map stays fully usable as a
  static, scrubbable map
- The scrubber is a real `<input type="range">` and announces a date
  (`aria-valuetext`), not a raw index
- The calendar strip is a `grid` with a **roving tabindex** — exactly one
  tabbable cell at a time rather than 2557 tab stops — with arrow/week/month/
  Home/End navigation, and each cell names its date and operation count. The
  playhead carries `aria-current="date"` and the trail window `aria-selected`,
  so "which day is now" is available non-visually
- **A "Zones in view" list** gives a keyboard and screen-reader route to every
  operation record. The polygons are drawn to a canvas, so they are pixels
  rather than focusable nodes; without the list, which zones were sprayed, with
  what, and whether an operation was *cancelled* would all be pointer-only
- Page Up / Page Down on the scrubber really do step a week — a native range
  steps ~1/10 of its span (~36 days here), so the behaviour is implemented
  rather than the instruction reworded
- **Reflows at 400% zoom.** Below 480px the fixed full-screen layout becomes an
  ordinary scrolling document; at the 320×256 test viewport every focusable
  control is reachable
- `aria-live` announcements are throttled and suppressed during playback; at 4×
  the date changes ~15 times a second and would otherwise make the page
  unusable with a screen reader
- Color encodings use CVD-safe palettes and are always backed by text; status
  additionally carries a non-color signal

## License

[MIT](LICENSE).
