"""Turn the district's spray-alert HTML into Operation records.

The source page is a DotNetNuke CMS page whose body is hand-authored HTML.  It
is therefore irregular in every way hand-authored HTML is irregular, and the
comments below name each irregularity that has actually been observed on the
live page rather than defending against hypotheticals.

Document shape
--------------
Inside one ``div.DNNModuleContent`` (the only one of seven that contains map
links) sits a two-level nested list per section::

    <h2>Current Scheduled Spray Operations</h2>
    <ul>
      <li>  OPERATION HEADER: method, target, product(s), date, time window,
            and occasionally an operation-level status
        <ul>
          <li> AREA: name, boundary description, "See Map" link carrying the
               Google My Maps id, and usually a status word </li>
          ...
        </ul>
      </li>
    </ul>
    <h2>Past Completed Spray Operations:</h2>
    <ul> ... </ul>
    <ul> ... </ul>          <-- yes, TWO sibling <ul>s.  See _iter_operations.

One Operation dict is emitted **per area**, so a header with three areas
yields three records that share the header fields.

Public API (fixed by the project's module contract)::

    parse_operations(html, *, source) -> list[dict]
    parse_operation_header(text) -> dict
    normalize_status(text) -> tuple[str, str | None]

Everything here is pure: no network, no clock beyond ``first_seen``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: bs4 backend.  html.parser is deliberate: it is stdlib, so the archiver has
#: no compiled dependency, and it is lenient about the unclosed tags DNN emits.
PARSER = "html.parser"

#: Tags that may carry an area name.  The module mixes both spellings
#: (219 <strong> vs 18 <b> on the page as fetched), so both must be honoured.
BOLD_TAGS = frozenset({"b", "strong"})

HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

#: Inline tags that plausibly wrap a status word.  Used to find the status
#: element precisely (so it can be removed before the boundary text is read)
#: instead of subtracting strings.
INLINE_TAGS = frozenset({"span", "strong", "b", "em", "i", "u", "font", "mark"})

MAP_URL_TEMPLATE = "https://www.google.com/maps/d/viewer?mid={mid}"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

#: Only ever used as a stop-word set when trimming a product phrase -- the
#: parser reads the calendar date out of DATE_RE, never out of the weekday.
WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------

#: "Aerial spraying" / "Ground spraying".  Matched on text, never on tag
#: position: the <strong> boundaries around the phrase move between entries.
METHOD_RE = re.compile(r"\b(aerial|ground)\s+spray", re.I)

#: "for adult mosquito control" / "for larval mosquito control".  Anchored on
#: "for " so the nearby "for the morning of" cannot be mistaken for a target.
TARGET_RE = re.compile(r"\bfor\s+(adult|larval|larvae)\b", re.I)

#: "using <products> is scheduled".  The optional comma covers two headers
#: that read "using Dibrom(R), is scheduled".  DOTALL because <br/> and raw
#: newlines appear inside the phrase.
#:
#: The verb alternation is load-bearing.  Anchoring only on "is scheduled"
#: makes the lazy group fall through to "$" on the handful of 2020 headers
#: that read "using Dibrom(R) is rescheduled for the evening of Sunday, July
#: 12, 2020 between 8:30 p.m. ...".  The whole sentence then reaches the comma
#: splitter and every clause becomes a "product" ("July 12", "depending on
#: weather", "10:00 p.m").  Terminating on any of the verbs that can follow
#: the product phrase keeps the capture to the products themselves.
PRODUCT_RE = re.compile(
    r"\busing\b(.*?)(?:"
    r",?\s*\b(?:is|are|was|were|will)\b"
    r"|\bfor\s+the\s+(?:morning|evening|afternoon|night)\b"
    r"|$)",
    re.I | re.S,
)

#: Belt-and-braces stop for anything the verb alternation above lets through:
#: a product name never contains a month, a weekday, a clock time or the word
#: "between", so the capture is truncated at the first one that appears.
PRODUCT_STOP_RE = re.compile(
    r"\b(?:" + "|".join(MONTHS) + r"|" + "|".join(WEEKDAYS) + r"|between|depending"
    r"|\d{1,2}:\d{2}\s*[ap]\.?m|\d{4})\b",
    re.I,
)

#: Separators inside a product list.  The order of the two named products is
#: NOT stable across entries ("Evergreen 5-25 and/or DeltaGard" vs "DeltaGard
#: and/or Evergreen 5-25"), so source order is preserved and nothing is keyed
#: on the products list.
PRODUCT_SPLIT_RE = re.compile(r"\s*(?:\band/or\b|\bor\b|\band\b|&|,)\s*", re.I)

#: Canonical pesticide table.  The district spells the same product a dozen
#: ways across six years of postings -- "Evergreen 5-25", "Evergreen 525",
#: "EverGreen ULV 5-25 Air", "Evergreen 5-25 ULV Air" are one product, as are
#: "VectoBac WDG" / "Vectobac WDG larvacide".  Matching on a distinctive
#: substring collapses them so the map can filter by product without showing
#: twelve near-duplicate entries.  Order matters only in that the first
#: matching pattern wins.
#:
#: Active ingredients are recorded because they are the part the public
#: actually cares about, and they are what the district's own product pages
#: and the EPA labels key on.
PRODUCT_CANON: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(r"evergreen", re.I), "Evergreen 5-25", "pyrethrins + PBO", "adulticide"),
    (re.compile(r"delta\s*gard", re.I), "DeltaGard", "deltamethrin", "adulticide"),
    (re.compile(r"dibrom", re.I), "Dibrom", "naled", "adulticide"),
    (re.compile(r"pyronyl", re.I), "Pyronyl 525", "pyrethrins + PBO", "adulticide"),
    (re.compile(r"vecto\s*bac", re.I), "VectoBac WDG", "Bti", "larvicide"),
    (re.compile(r"altosid", re.I), "Altosid", "methoprene", "larvicide"),
)


def canonical_product(raw: str) -> str | None:
    """Map a raw product string onto its canonical name, or None if unknown.

    Returning None rather than the raw string is deliberate: the caller keeps
    the verbatim text in ``products_raw``, so an unrecognised name is still
    recoverable, while ``products`` stays a small closed vocabulary that the
    frontend can build a legend from.
    """
    for pattern, name, _ingredient, _kind in PRODUCT_CANON:
        if pattern.search(raw):
            return name
    return None

#: Trademark/registered marks to strip from product names.
TRADEMARK_RE = re.compile("[\u00ae\u2122\u00a9]")  # registered, trademark, copyright

PERIOD_RE = re.compile(r"\bfor the (morning|evening|afternoon|night)\s+of\b", re.I)

#: "July 23, 2026" and also "July 2 2026" (two headers omit the comma; a regex
#: that requires it silently loses nine rows).  Case-insensitive because the
#: postponement dates are shouted in caps: "POSTPONED TO MONDAY, JULY 27, 2026".
DATE_RE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(\d{4})\b",
    re.I,
)

#: "8:40 p.m.", "3:15 a.m.", "8:40&nbsp;p.m." -- the spacing and the dots are
#: both optional in practice, hence the loose separators.
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?", re.I)

#: Status keywords, matched CASE-SENSITIVELY.  Requiring capitals is what
#: keeps the lowercase editorial note "(rescheduled from Thursday, July 9,
#: 2026)" from being read as a status.
STATUS_KEYWORD_RE = re.compile(r"\b(?:COMPLETED|COMPLETE|POSTPONED|CANCELL?ED|RESCHEDULED)\b")

#: A status keyword plus everything after it that is not lowercase, so
#: "POSTPONED TO MONDAY, JULY 27, 2026" is captured verbatim, not just the
#: leading word.
STATUS_PHRASE_RE = re.compile(
    r"\b(?:COMPLETED|COMPLETE|POSTPONED|CANCELL?ED|RESCHEDULED)\b[^a-z]*"
)

#: Case-insensitive fallback, used only when a caller hands
#: :func:`normalize_status` a phrase it has already identified as a status.
STATUS_KEYWORD_ANY_CASE_RE = re.compile(
    r"\b(?:completed|complete|postponed|cancell?ed|rescheduled)\b", re.I
)

#: The editorial note "(rescheduled from Thursday, July 9, 2026)".  It sits
#: exactly where boundary text would and is not a status.
RESCHEDULED_NOTE_RE = re.compile(r"\((?:re)?scheduled\s+from[^)]*\)", re.I)

#: Google My Maps id out of any href shape.  The path varies -- /u/0/edit,
#: /u/1/edit, /u/3/edit, /edit -- so only the query parameter is trusted.
MID_RE = re.compile(r"[?&]mid=([A-Za-z0-9_\-]+)")

#: Section headings, classified by keyword rather than by exact string.
PAST_RE = re.compile(r"\bpast\b|\bcompleted\b|\bprevious(ly)?\b|\barchive", re.I)
CURRENT_RE = re.compile(r"\bcurrent\b|\bupcoming\b|\bscheduled\b", re.I)

#: Boundary fragments are written ">North to X.", but the marker is sometimes
#: missing, sometimes followed by a space, and the direction is sometimes a
#: single letter.
SINGLE_LETTER_DIR_RE = re.compile(r"^([NSEW])\s+to\b")
DIRECTION_WORDS = {"N": "North", "S": "South", "E": "East", "W": "West"}

#: Text nodes that may legally appear *between* two bold runs of an area name
#: (a stray colon or dash the author typed outside the bold tags).
NAME_RUN_SEPARATORS = frozenset({":", "-", "\u2013", "\u2014", ".", ";", ",", ":-"})

#: Statistics from the most recent :func:`parse_operations` call.  The runner
#: folds these into ``manifest.errors`` / the manifest counters.  Kept as a
#: module attribute rather than a second return value so the fixed API
#: signature is preserved.
LAST_PARSE_STATS: dict[str, Any] = {}


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def clean(s: str | None) -> str:
    """Collapse whitespace and normalise the punctuation BeautifulSoup hands us.

    The page contains 330 ``&nbsp;`` entities.  bs4 decodes those to U+00A0,
    which ``str.strip()`` does not remove and which would otherwise survive
    into area names ("West Terminous Tract\\u00a0") and product lists.  The
    explicit replace runs before the ``\\s+`` collapse as belt and braces.
    """
    if not s:
        return ""
    s = (
        s.replace("\u00a0", " ")   # NBSP: str.strip() does not remove this
        .replace("\u2019", "'")    # curly apostrophe -> ASCII
        .replace("\u200b", "")     # zero-width space
    )
    return re.sub(r"\s+", " ", s).strip()


def _to_24h(hour: str, minute: str, meridiem: str) -> str:
    """Convert a 12-hour clock reading to a zero-padded 24-hour ``HH:MM``."""
    h = int(hour)
    if meridiem.lower() == "a":
        h = 0 if h == 12 else h          # 12:20 a.m. -> 00:20
    else:
        h = 12 if h == 12 else h + 12    # 12:20 p.m. -> 12:20, 8:40 p.m. -> 20:40
    return f"{h:02d}:{int(minute):02d}"


def _iso_date(match: re.Match[str]) -> str | None:
    """Build ``YYYY-MM-DD`` from a :data:`DATE_RE` match, or ``None`` if the
    day-of-month is out of range for that month (a typo we do not guess at)."""
    month = MONTHS[match.group(1).lower()]
    day = int(match.group(2))
    year = int(match.group(3))
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _first_date(text: str) -> tuple[str | None, str | None]:
    """Return ``(iso_date, verbatim_text)`` for the FIRST date in ``text``.

    Two headers carry two dates.  In one the second date is the postponement
    target ("POSTPONED TO MONDAY, JULY 27, 2026"); in the other it is a
    weather contingency ("June 29, 2026 OR Tuesday, June 30, 2026 (depending
    on weather)").  The first match is the scheduled date in both cases.
    """
    for m in DATE_RE.finditer(text):
        iso = _iso_date(m)
        if iso:
            return iso, m.group(0)
    return None, None


def _utcnow_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def normalize_status(text: str | None) -> tuple[str, str | None]:
    """Classify a status phrase.

    Accepts either an isolated phrase ("COMPLETE") or a larger blob that may
    contain one (a whole operation header, an area's text).  Returns
    ``(status_enum, verbatim_phrase)`` where the enum is one of
    ``scheduled | complete | postponed | cancelled`` and the verbatim phrase is
    exactly what the page said, or ``None`` when no status was found.

    Two ordering rules matter and are both load-bearing:

    * ``CANCEL`` is tested *before* ``RESCHEDULED`` because the live page says
      "CANCELLED DUE TO TECHNICAL ISSUE - WILL BE RESCHEDULED", which contains
      both words and is a cancellation.
    * The all-caps phrase is preferred over a case-insensitive keyword scan,
      so the lowercase note "(rescheduled from ...)" is not a status.
    """
    if not text:
        return "scheduled", None

    blob = clean(text)
    if not blob:
        return "scheduled", None

    phrase_match = STATUS_PHRASE_RE.search(blob)
    if phrase_match:
        phrase = phrase_match.group(0)
    else:
        # No shouted phrase.  The caller may still have handed us a status it
        # already identified but that is not in capitals, so accept a string
        # that *begins* with a status keyword ("Complete", "CANCELED").
        #
        # The anchor matters: a mere search() would classify the lowercase
        # editorial note "(rescheduled from Thursday, July 9, 2026)" -- and any
        # area blob containing it -- as "postponed", which is wrong.  That note
        # is a rescheduling *provenance*, not this entry's status.
        if not STATUS_KEYWORD_ANY_CASE_RE.match(blob):
            return "scheduled", None
        phrase = blob

    # Trailing separators/whitespace are artefacts of the greedy [^a-z]* run.
    phrase = clean(phrase).strip(" \t.,;:-")
    if not phrase:
        return "scheduled", None

    upper = phrase.upper()
    if "CANCEL" in upper:
        return "cancelled", phrase
    if "POSTPON" in upper:
        return "postponed", phrase
    if "RESCHEDUL" in upper:
        return "postponed", phrase
    if "COMPLET" in upper:
        return "complete", phrase
    return "scheduled", None


def _resolve_date(header_date: str | None, status: str, status_text: str | None) -> str | None:
    """Apply the postponement override to an operation's date.

    A postponed entry keeps announcing its *original* date in the header and
    names the new one only inside the status ("POSTPONED TO MONDAY, JULY 27,
    2026").  The date that matters for a map timelapse is when the spray
    actually happened, so a POSTPONED status carrying a parseable date wins.

    Anything else -- a cancellation, a postponement with no date -- keeps the
    header date.
    """
    if status != "postponed" or not status_text:
        return header_date
    if not status_text.upper().lstrip().startswith(("POSTPONED", "RESCHEDULED")):
        return header_date
    new_date, _ = _first_date(status_text)
    return new_date or header_date


# --------------------------------------------------------------------------
# Operation header
# --------------------------------------------------------------------------


def parse_operation_header(text: str) -> dict:
    """Parse the header sentence of one operation.

    ``text`` is the outer ``<li>``'s own text with the nested area list
    removed, e.g.::

        Ground spraying for adult mosquito control using Evergreen 5-25 and/or
        DeltaGard(R) is scheduled for the morning of Friday, July 24, 2026,
        between 3:15 a.m. and 6:00 a.m.

    Returns a dict with ``method target products date date_text period
    time_start time_end status status_text header_text``.  Every field is
    independently optional: a header missing its time window still yields the
    date, and vice versa.  Nothing raises.
    """
    header = clean(text)

    method_match = METHOD_RE.search(header)
    method = method_match.group(1).lower() if method_match else None

    target_match = TARGET_RE.search(header)
    target = None
    if target_match:
        # "larval" and "larvae" both appear in the wild; collapse to "larval".
        target = "larval" if target_match.group(1).lower().startswith("larv") else "adult"

    products, products_raw = _parse_products(header)

    period_match = PERIOD_RE.search(header)
    period = period_match.group(1).lower() if period_match else None

    times = TIME_RE.findall(header)
    time_start = _to_24h(*times[0]) if len(times) >= 1 else None
    time_end = _to_24h(*times[1]) if len(times) >= 2 else None

    date, date_text = _first_date(header)

    status, status_text = normalize_status(header)

    return {
        "method": method,
        "target": target,
        "products": products,
        "products_raw": products_raw,
        "date": date,
        "date_text": date_text,
        "period": period,
        "time_start": time_start,
        "time_end": time_end,
        "status": status,
        "status_text": status_text,
        "header_text": header,
    }


def _parse_products(header: str) -> tuple[list[str], list[str]]:
    """Extract the product list from "... using <products> is scheduled ...".

    Returns ``(canonical, raw)``:

      * ``raw`` preserves the district's own wording and source order (the
        "and/or" operands are not written in a stable order) with internal
        punctuation intact -- "Altosid concentrate (SR-20)" must survive
        whole, so splitting on "-" or "(" is not an option.
      * ``canonical`` collapses six years of spelling drift onto the closed
        vocabulary in :data:`PRODUCT_CANON`, de-duplicated, so the frontend
        can offer a product filter with one entry per actual pesticide.

    A token that matches nothing in the canonical table is kept in ``raw``
    and simply omitted from ``canonical`` -- silently inventing a new
    canonical product from an unrecognised fragment is how the garbage
    ("July 12", "depending on weather") got into the archive in the first
    place.
    """
    match = PRODUCT_RE.search(header)
    if not match:
        return [], []
    phrase = match.group(1)

    # Truncate at the first month / weekday / clock time that survived the
    # verb alternation, so a run-on header cannot leak a sentence in here.
    stop = PRODUCT_STOP_RE.search(phrase)
    if stop:
        phrase = phrase[: stop.start()]

    raw_text = clean(phrase)
    if not raw_text:
        return [], []

    raw: list[str] = []
    for token in PRODUCT_SPLIT_RE.split(raw_text):
        # The (R) can sit mid-string ("Evergreen 5-25(R) ULV Air"), so strip
        # the character wherever it appears rather than only at the end.
        name = clean(TRADEMARK_RE.sub("", token)).strip(" .,;:/")
        if name and name.lower() not in {"the", "a", "an"}:
            raw.append(name)

    canonical: list[str] = []
    for name in raw:
        canon = canonical_product(name)
        if canon and canon not in canonical:
            canonical.append(canon)
    return canonical, raw


# --------------------------------------------------------------------------
# Section headings
# --------------------------------------------------------------------------


def section_kind(heading: str | None) -> str | None:
    """Classify a heading as ``"current"``, ``"past"`` or ``None`` (unknown).

    ``None`` means "leave the running section unchanged" -- the container also
    holds an ``<h1>Spray Alerts</h1>`` that must not reset anything.

    "past" is tested first on purpose.  Today's heading, "Past Completed Spray
    Operations:", contains neither "current" nor "scheduled", but a future
    rewording to "Past Scheduled Operations" would be filed under *current* by
    a current-first test.
    """
    h = heading or ""
    if PAST_RE.search(h):
        return "past"
    if CURRENT_RE.search(h):
        return "current"
    return None


# --------------------------------------------------------------------------
# DOM navigation
# --------------------------------------------------------------------------


def _has_mid_link(node: Tag) -> bool:
    """True when ``node`` contains at least one anchor carrying a map id."""
    return any(MID_RE.search(a.get("href", "")) for a in node.find_all("a", href=True))


def _outermost_lists(soup: BeautifulSoup) -> list[Tag]:
    """Find every top-level ``<ul>`` that holds spray operations.

    Rather than trusting a CMS-generated class or module id (``dnn_ctr424`` is
    an instance number and will change), the lists are located by content:
    start from the anchors that carry a map id, walk up to the outermost
    enclosing ``<ul>``, and de-duplicate in document order.  This survives the
    ``div.DNNModuleContent`` / ``div.Normal`` wrappers being renamed or
    removed entirely.
    """
    # Prefer the module that actually holds map links, when it is identifiable;
    # this keeps an unrelated navigation list from ever being considered.
    scopes: list[Tag | BeautifulSoup] = [
        d for d in soup.select("div.DNNModuleContent") if _has_mid_link(d)
    ] or [soup]

    lists: list[Tag] = []
    for scope in scopes:
        for anchor in scope.find_all("a", href=True):
            if not MID_RE.search(anchor.get("href", "")):
                continue
            top: Tag | None = None
            for parent in anchor.parents:
                if not isinstance(parent, Tag):
                    break
                if parent.name in ("ul", "ol"):
                    top = parent            # keep climbing; we want the outermost
                elif parent.name in ("body", "html") or parent is scope:
                    break
            if top is not None and not any(top is seen for seen in lists):
                lists.append(top)
    return lists


def _container_groups(soup: BeautifulSoup) -> list[Tag]:
    """Return the parent elements that hold the operation lists, in order.

    Normally there is exactly one (the ``div.Normal`` inside the content
    module).  Returning a list keeps the walk correct if the two sections ever
    end up in different wrappers.
    """
    groups: list[Tag] = []
    for ul in _outermost_lists(soup):
        parent = ul.parent
        if isinstance(parent, Tag) and not any(parent is g for g in groups):
            groups.append(parent)
    return groups


def _iter_operations(soup: BeautifulSoup) -> list[tuple[str | None, Tag]]:
    """Yield ``(section, operation_li)`` for every operation on the page.

    This is where the page's nastiest structural trap lives: the "Past"
    heading is followed by **two sibling ``<ul>`` elements**, not one.  The
    obvious ``heading.find_next_sibling("ul")`` returns only the first and
    silently drops four operation rows and two map ids (the May 2026 entries).

    The correct walk is therefore: iterate the container's direct children in
    document order, carry a "current section" variable, update it only on a
    *recognised* heading, and consume every ``<ul>`` encountered until the
    next recognised heading.
    """
    result: list[tuple[str | None, Tag]] = []

    for container in _container_groups(soup):
        children = [c for c in container.children if isinstance(c, Tag)]
        # Strict mode when real heading tags exist; otherwise fall back to
        # treating short block elements as headings.  The fallback is gated so
        # the ordinary <p> that sits between the two sections can never be
        # mistaken for a heading.
        has_headings = any(c.name in HEADING_TAGS for c in children)

        kind: str | None = None
        for child in children:
            is_heading = child.name in HEADING_TAGS or (
                not has_headings
                and child.name in ("p", "div", "strong", "b")
                and len(clean(child.get_text())) <= 120
            )
            if is_heading:
                found = section_kind(child.get_text())
                if found:
                    kind = found
                continue

            if child.name != "ul":
                continue

            # recursive=False is mandatory: without it the nested area <li>s
            # are also returned and get parsed as operations.
            for op_li in child.find_all("li", recursive=False):
                result.append((kind, op_li))

    return result


def _header_text(op_li: Tag) -> str:
    """Text of an operation's own header, excluding its nested area list.

    ``op_li.get_text()`` would include every area's name and boundary, so the
    direct children are walked instead and the walk stops at the nested list.

    An *empty* ``<ul></ul>`` does not count as that terminator.  One archived
    operation opens with a stray empty list before its own text::

        <li> <ul> </ul> G<strong>round spraying </strong>for adult ...

    Breaking on it yields an empty header, which fails the date parse and
    discards a perfectly good area.  Only a list that actually contains an
    ``<li>`` ends the header.
    """
    parts: list[str] = []
    for node in op_li.children:
        if isinstance(node, Tag):
            if node.name in ("ul", "ol"):
                if node.find("li") is not None:
                    break
                continue          # stray empty list: skip, keep reading
            parts.append(node.get_text())
        elif isinstance(node, NavigableString):
            parts.append(str(node))
    return clean("".join(parts))


def _area_items(op_li: Tag) -> list[Tag]:
    """Direct area ``<li>``s of an operation, across all its nested lists."""
    areas: list[Tag] = []
    for sub in op_li.find_all(("ul", "ol"), recursive=False):
        areas.extend(sub.find_all("li", recursive=False))
    return areas


# --------------------------------------------------------------------------
# Area fields
# --------------------------------------------------------------------------


def _extract_mid(li: Tag) -> str | None:
    """Pick the map id of the area's real "See Map" link.

    Several areas carry more than one anchor, because an editor pasted a new
    link and left the old one behind wrapping a coloured space.  Position is
    NOT a reliable tie-breaker: on the current live page the good "See Map"
    anchor comes first and the stray blank one second, but in Wayback
    snapshots the order is reversed --

        <a href="...mid=1LbMmKcckTL0..."> </a><a href="...mid=1k5QybTnt...">See Map</a>

    -- so "first anchor wins" silently attaches the *wrong polygon* to four
    areas per snapshot in the archive.  That is worse than dropping the row:
    the map would draw a confidently incorrect spray area.

    The anchor is therefore chosen by what the reader actually sees:

    1. an anchor whose visible text mentions "map" (the real link);
    2. failing that, any anchor with visible text (a blank anchor is debris);
    3. failing that, the first anchor carrying an id at all.

    On the live page this returns exactly what position-based selection did.
    """
    candidates: list[tuple[int, str]] = []
    for anchor in li.find_all("a", href=True):
        m = MID_RE.search(anchor.get("href", ""))
        if not m:
            continue
        text = clean(anchor.get_text())
        if re.search(r"\bmaps?\b", text, re.I):
            rank = 0
        elif text:
            rank = 1
        else:
            rank = 2
        candidates.append((rank, m.group(1)))

    if not candidates:
        return None
    # min() is stable, so ties fall back to document order.
    return min(candidates, key=lambda c: c[0])[1]


def _leading_bold_run(li: Tag) -> list[Tag]:
    """Collect the contiguous leading run of ``<strong>``/``<b>`` tags.

    The area name is bold, but the authors put the colon in five different
    places (inside the bold, after it, in a second bold, in a ``<b>`` next to a
    ``<strong>``, or nowhere because the name is not bold at all).  Taking the
    *run* rather than the first tag handles all of them, and stopping at the
    first non-empty non-bold text node prevents the whitespace-only ``<b> </b>``
    that follows the "(rescheduled from ...)" note from being glued on.
    """
    run: list[Tag] = []
    for node in li.children:
        if isinstance(node, NavigableString):
            text = clean(str(node))
            if not text:
                continue                      # whitespace between tags
            if text in NAME_RUN_SEPARATORS:
                continue                      # a colon typed outside the bold
            break                             # real prose: the name has ended
        if not isinstance(node, Tag):
            continue
        if node.name in BOLD_TAGS:
            run.append(node)
            continue
        if node.name == "br":
            continue
        if not clean(node.get_text()):
            continue                          # empty inline tag, e.g. <em></em>
        break                                 # "See Map" anchor or boundary span
    return run


def _fallback_name(full_text: str) -> str:
    """Area name for the one entry with no bold markup at all.

    Everything before the first colon, falling back to everything before the
    first boundary marker.  Safe only because the unbolded area has no
    *internal* colon -- two bolded areas do ("... Weston Ranch area: West
    block"), which is exactly why the bold run is tried first.
    """
    if ":" in full_text:
        return full_text.split(":", 1)[0]
    if ">" in full_text:
        return full_text.split(">", 1)[0]
    return full_text


def _find_status_tag(li: Tag, exclude: list[Tag]) -> tuple[Tag | None, str | None]:
    """Locate the inline element whose entire text is an all-caps status.

    Preferring an element over a regex means the status can be *removed* from
    the tree before the boundary text is read, instead of subtracted from a
    string afterwards.  Elements inside the area-name run are excluded so an
    all-caps place name could never be mistaken for a status.
    """
    excluded_ids = {id(tag) for tag in exclude}
    for tag in exclude:
        excluded_ids.update(id(d) for d in tag.descendants if isinstance(d, Tag))

    for tag in li.find_all(list(INLINE_TAGS)):
        if id(tag) in excluded_ids:
            continue
        text = clean(tag.get_text())
        if not text or re.search(r"[a-z]", text):
            continue                          # must be shouted to count
        if STATUS_KEYWORD_RE.search(text):
            # find_all is document order, so the first hit is the outermost
            # wrapper (<span> around <strong>); decomposing it takes both.
            return tag, text
    return None, None


def _normalize_boundary(text: str) -> str | None:
    """Turn the raw boundary description into clean prose, or ``None``.

    Input looks like ``">North to Watercourse St., >South to French Camp Rd."``
    with any of these deviations, all observed live: a missing ">" on one
    fragment, a space after the ">", single-letter directions ("N to Eight Mile
    Rd."), a missing comma between fragments, a doubled period ("Cox Rd.."),
    and four entries that are free prose with no markers at all.

    Returns ``None`` -- never ``""`` -- when nothing is left, which is the case
    for the aerial/island areas that describe no boundary.
    """
    t = clean(text)
    # The "(rescheduled from ...)" editorial note is not a boundary.
    t = clean(RESCHEDULED_NOTE_RE.sub(" ", t))
    if not t:
        return None

    # Force a split point before every marker even when the author omitted the
    # separating space or comma.
    t = t.replace(">", " >")
    fragments: list[str] = []
    for raw in t.split(">"):
        # ':' is in the strip set because a colon typed outside the bold name
        # tag lands at the head of the boundary text.  '.' is NOT: it is part
        # of "Rd." and stripping it would mangle every street abbreviation.
        frag = raw.strip(" \t,;:")
        if not frag:
            continue
        frag = SINGLE_LETTER_DIR_RE.sub(
            lambda m: DIRECTION_WORDS[m.group(1)] + " to", frag
        )
        frag = re.sub(r"\.{2,}", ".", frag)     # "Cox Rd.." -> "Cox Rd."
        fragments.append(frag)

    return ", ".join(fragments) or None


def _parse_area(li: Tag) -> dict:
    """Extract ``area_name``, ``boundary_text``, ``mid`` and status from one area.

    Works on a detached copy of the element so consumed nodes (the name tags,
    the map anchors, the status span) can simply be ``decompose()``d.  Removing
    nodes is far more robust than trying to subtract their text from a string,
    because the same characters recur in the parts we want to keep.
    """
    copy = BeautifulSoup(str(li), PARSER).find("li")
    if copy is None:                          # pathological markup; degrade
        copy = BeautifulSoup(f"<li>{li.get_text()}</li>", PARSER).find("li")

    full_text = clean(copy.get_text())

    # 1. The map id must be read BEFORE anything is removed: one area hides
    #    its anchor *inside* the bold name run.
    mid = _extract_mid(copy)

    # 2. Area name from the leading bold run, else text before the colon.
    name_tags = _leading_bold_run(copy)
    if name_tags:
        name = clean("".join(t.get_text() for t in name_tags))
    else:
        name = clean(_fallback_name(full_text))
    area_name = re.sub(r"\s*:\s*$", "", name).strip()

    # 3. Status: prefer the shouted element, fall back to a regex on the text.
    status_tag, status_raw = _find_status_tag(copy, name_tags)
    if status_raw is None:
        remainder = full_text
        if area_name:
            remainder = remainder.replace(area_name, " ", 1)
        _, status_raw = normalize_status(remainder)
    status, status_text = normalize_status(status_raw)

    # 4. Boundary: whatever text survives once name, links and status are gone.
    for tag in name_tags:
        tag.decompose()
    for anchor in copy.find_all("a"):
        anchor.decompose()
    if status_tag is not None:
        status_tag.decompose()

    leftover = clean(copy.get_text())
    if status_tag is None and status_text:
        # Status was found by regex, so it is still sitting in the text.
        leftover = clean(leftover.replace(status_text, " ", 1))
    boundary_text = _normalize_boundary(leftover)

    return {
        "area_name": area_name or None,
        "boundary_text": boundary_text,
        "mid": mid,
        "status": status,
        "status_text": status_text,
        "full_text": full_text,
    }


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def parse_operations(html: str, *, source: str) -> list[dict]:
    """Parse a spray-alert page into one Operation dict per sprayed area.

    Parameters
    ----------
    html:
        Full page source, live or from a Wayback snapshot.
    source:
        Provenance tag stored on every record: ``"live"`` or
        ``"wayback:<timestamp>"``.

    Returns
    -------
    list[dict]
        Records matching the project data contract.  Areas that carry no map
        link are skipped (they have no geometry to draw) and operations whose
        date cannot be parsed are skipped entirely -- both are counted in
        :data:`LAST_PARSE_STATS` and logged, never raised, so one malformed
        entry cannot cost us the other 78 records.

    Notes
    -----
    Records are returned in document order, which is *not* date order: the
    live page interleaves July 10 operations between two July 9 ones.  Callers
    must sort explicitly rather than inferring anything from position.
    """
    stats: dict[str, Any] = {
        "operations_seen": 0,
        "operations_skipped_no_date": 0,
        "operations_skipped_no_areas": 0,
        "areas_seen": 0,
        "areas_skipped_no_mid": 0,
        "rows": 0,
        "errors": [],
    }

    try:
        soup = BeautifulSoup(html or "", PARSER)
    except Exception as exc:  # noqa: BLE001 - a parser blow-up must not kill the run
        msg = f"{source}: could not parse HTML: {exc}"
        log.error(msg)
        stats["errors"].append(msg)
        LAST_PARSE_STATS.clear()
        LAST_PARSE_STATS.update(stats)
        return []

    operations = _iter_operations(soup)
    if not operations:
        msg = f"{source}: no operation lists found (page layout may have changed)"
        log.warning(msg)
        stats["errors"].append(msg)

    first_seen = _utcnow_iso()
    rows: list[dict] = []

    for section, op_li in operations:
        stats["operations_seen"] += 1
        try:
            header = parse_operation_header(_header_text(op_li))
        except Exception as exc:  # noqa: BLE001 - isolate a single bad header
            msg = f"{source}: header parse failed: {exc}"
            log.warning(msg)
            stats["errors"].append(msg)
            continue

        if not header["date"]:
            # A bogus date would corrupt every id and the whole timelapse, so
            # the operation is dropped rather than guessed at.
            stats["operations_skipped_no_date"] += 1
            msg = (
                f"{source}: skipped operation with unparseable date: "
                f"{header['header_text'][:160]!r}"
            )
            log.warning(msg)
            stats["errors"].append(msg)
            continue

        areas = _area_items(op_li)
        if not areas:
            stats["operations_skipped_no_areas"] += 1
            msg = (
                f"{source}: operation on {header['date']} has no area list: "
                f"{header['header_text'][:120]!r}"
            )
            log.warning(msg)
            stats["errors"].append(msg)
            continue

        for area_li in areas:
            stats["areas_seen"] += 1
            try:
                area = _parse_area(area_li)
            except Exception as exc:  # noqa: BLE001 - isolate a single bad area
                msg = f"{source}: area parse failed on {header['date']}: {exc}"
                log.warning(msg)
                stats["errors"].append(msg)
                continue

            if not area["mid"]:
                # No map link means no polygon; the row would be undrawable
                # and has no stable id.  Counted so it shows up in the manifest.
                stats["areas_skipped_no_mid"] += 1
                msg = (
                    f"{source}: skipped area with no map link on {header['date']}: "
                    f"{(area['area_name'] or area['full_text'])[:120]!r}"
                )
                log.warning(msg)
                stats["errors"].append(msg)
                continue

            # Area status wins; otherwise the area inherits the operation's
            # status (one aerial operation is postponed as a whole and its two
            # areas say nothing); otherwise "scheduled".
            if area["status_text"]:
                status, status_text = area["status"], area["status_text"]
            elif header["status_text"]:
                status, status_text = header["status"], header["status_text"]
            else:
                status, status_text = "scheduled", None

            date = _resolve_date(header["date"], status, status_text)
            if not date:
                stats["operations_skipped_no_date"] += 1
                continue

            rows.append(
                {
                    "id": f"{date}|{area['mid']}",
                    "date": date,
                    "method": header["method"],
                    "target": header["target"],
                    "products": header["products"],
                    "products_raw": header["products_raw"],
                    "period": header["period"],
                    "time_start": header["time_start"],
                    "time_end": header["time_end"],
                    "area_name": area["area_name"],
                    "boundary_text": area["boundary_text"],
                    "mid": area["mid"],
                    "map_url": MAP_URL_TEMPLATE.format(mid=area["mid"]),
                    "status": status,
                    "status_text": status_text,
                    "section": section,
                    "source": source,
                    "first_seen": first_seen,
                }
            )

    stats["rows"] = len(rows)
    LAST_PARSE_STATS.clear()
    LAST_PARSE_STATS.update(stats)

    log.info(
        "%s: %d operations -> %d rows (%d areas, %d without a map link, "
        "%d operations skipped for a bad date)",
        source,
        stats["operations_seen"],
        stats["rows"],
        stats["areas_seen"],
        stats["areas_skipped_no_mid"],
        stats["operations_skipped_no_date"],
    )
    return rows
