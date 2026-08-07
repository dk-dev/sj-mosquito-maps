# Accessibility conformance report

**Product:** San Joaquin Mosquito Spray Timelapse (`index.html`, single-page map application)
**Standard:** Web Content Accessibility Guidelines (WCAG) 2.2, Level AA
**Report date:** 2026-07-31
**Status:** Conforms to WCAG 2.2 Level AA, with the documented exception and
open items listed under [Known limitations](#known-limitations).

California's [state web standards](https://webstandards.ca.gov/website-accessibility/)
require *"Web Content Accessibility Guidelines (WCAG) 2.2, Level AA Guidelines and
Success Criteria"*, under Government Code sections 7405, 11135 and 11546.7. This
project is a personal one and is not a state agency website, so the biennial
signed certification in 11546.7 does not bind it. The technical standard is met
anyway, because the natural home for this data is a public-health agency, and
that is a bad time to discover the work was never done.

---

## How this was tested

Every value in this report is a **rendered** measurement — `getComputedStyle`,
`getBoundingClientRect`, `document.elementFromPoint`, real key events — not a
reading of the stylesheet. The two disagree constantly once alpha compositing
and inheritance are involved, and this report is only worth what its method is.

Colour contrast over the map was measured against **real basemap tiles** fetched
from the live services and composited in the same order the browser does
(tiles → scrim → casing → core), sampling the county at multiple zooms rather
than assuming a representative background colour.

| | |
|---|---|
| Browser | Chrome 150 |
| Served by | `serve.py` over `http://localhost` |
| Viewports | 1280×860, 800×600, 768×500, 700×560, 645×446, 520×560, 481×580, 320×256 |
| Basemaps | all five, independently |
| Assistive technology | **Not tested with a screen reader.** See below. |

**No screen reader has been used to test this.** California tests with JAWS and
NVDA in Chrome; until that is done, every claim here about how something is
*announced* is an inference, not an observation. This remains the largest single
gap in the report, and is tracked as an open issue.

A partial pass has been made against the **computed accessibility tree** — the
representation a screen reader actually consumes, as opposed to the markup that
produces it. That is a weaker check than listening, but it is a stronger one than
reading HTML, and it found two defects that structural review had passed:

- Filter checkboxes flattened their label and their count into a single token,
  so the accessible name of one read `Evergreen 5-25620`. Every control "had a
  name" — the earlier audit asked whether a name existed, not whether it was
  intelligible. Names are now set explicitly: `Evergreen 5-25, 620 operations`.
- The trail slider had no `aria-valuetext` and so announced a bare `7`, with
  the unit living only in a neighbouring element. It now announces `7 days`,
  matching the scrubber beside it.

Both were invisible on screen, because CSS separated what the accessibility tree
concatenated. That is the category of defect this gap exists to catch, and it is
a reason to expect a real screen-reader session to find more.

---

## Applicable criteria

Level A and AA. `4.1.1 Parsing` is omitted: it was removed from WCAG in 2.2.

### Perceivable

| SC | Level | Result | Evidence |
|---|---|---|---|
| 1.1.1 Non-text Content | A | Pass | No `<img>` content. The map carries `role="application"` and a descriptive name; the spray polygons are canvas pixels and have a full text equivalent in the **Zones in view** list, the calendar summary and the status region. |
| 1.2.x Time-based Media | A/AA | N/A | No audio or video. The timelapse is scripted animation, not a media element. |
| 1.3.1 Info and Relationships | A | Pass | Landmarks (`main`, four labelled `region`s), `role="grid"` calendar, headings h1→h2 with no skips, 0 duplicate ids, 0 dangling ARIA references. |
| 1.3.2 Meaningful Sequence | A | Pass | DOM order matches visual order in every layout. |
| 1.3.3 Sensory Characteristics | A | Pass | Playhead and trail state are conveyed by `aria-current="date"` and `aria-selected`, not by position or colour alone. |
| 1.3.4 Orientation | AA | Pass | No orientation lock. |
| 1.3.5 Identify Input Purpose | AA | N/A | No input collects information about the user. All controls are application state. |
| 1.4.1 Use of Color | A | Pass | Operation status is encoded by outline pattern (solid / dashed / dotted, scaled with stroke weight) as well as hue, and written out in the legend, popup and zone list. The calendar heat ramp is Viridis, monotonic in lightness, and every cell states its count in its accessible name. |
| 1.4.2 Audio Control | A | N/A | No audio. |
| 1.4.3 Contrast (Minimum) | AA | Pass | Worst measured text pair 4.92:1 (white on `#2a6cdb`), against a 4.5:1 requirement. Re-measured with every translucent panel composited over pure white — the worst case behind a light basemap — with no failures. |
| 1.4.4 Resize Text | AA | Pass | Verified at 320×256, equivalent to 400% of 1280px. |
| 1.4.5 Images of Text | AA | Pass | No author images of text. Place labels are baked into basemap tiles, which is essential map content, and five renderings are offered. |
| 1.4.10 Reflow | AA | Pass | At 320×256 the document does not scroll horizontally and every focusable control is reachable. Below 480px the fixed full-screen layout becomes an ordinary scrolling document. |
| 1.4.11 Non-text Contrast | AA | Pass | See [Contrast over the map](#contrast-over-the-map). |
| 1.4.12 Text Spacing | AA | Pass | No loss of content at the required spacing at desktop sizes. |
| 1.4.13 Content on Hover or Focus | AA | Pass | Dismissible: Escape closes an open tooltip (Leaflet handles only popups natively). Hoverable: Leaflet gives tooltips `pointer-events: none`, so moving the pointer toward one never removes hover from the polygon and the content does not vanish. Persistent: content remains until the pointer leaves, Escape, or the operation leaves the trail window. |

### Operable

| SC | Level | Result | Evidence |
|---|---|---|---|
| 2.1.1 Keyboard | A | Pass | Arrow keys pan the map, `+`/`−` zoom, Escape closes popups and tooltips. The calendar grid has arrow / week / month / Home / End navigation. Every operation record is reachable through the **Zones in view** buttons. |
| 2.1.2 No Keyboard Trap | A | Pass | Tab is never intercepted; no modal or focus loop. |
| 2.1.4 Character Key Shortcuts | A | Pass | The only single-key shortcut is Space, which is whitespace rather than a letter, number, punctuation or symbol, and is additionally scoped to the body and the map. |
| 2.2.1 Timing Adjustable | A | Pass | No time limit on any task. |
| 2.2.2 Pause, Stop, Hide | A | Pass | Playback exceeds five seconds but the Play/Pause control is the first item in the timeline dock, and `prefers-reduced-motion: reduce` suppresses autoplay entirely. |
| 2.3.1 Three Flashes | A | Pass | Maximum step rate is ~15 day-steps per second, but each polygon appears once and then fades monotonically; there is no repeated opposing luminance change on one area. |
| 2.4.1 Bypass Blocks | A | Pass | Skip link verified with a real Tab press, plus four labelled landmark regions. |
| 2.4.2 Page Titled | A | Pass | — |
| 2.4.3 Focus Order | A | Pass | Opening a zone popup from the **Zones in view** list moves focus into the popup and restores it to the originating button on close. |
| 2.4.4 Link Purpose | A | Pass | All links self-describing. |
| 2.4.5 Multiple Ways | AA | N/A | Single page, not a set of pages. |
| 2.4.6 Headings and Labels | AA | Pass | All 69 focusable controls carry an accessible name; measured count of unnamed controls is 0. |
| 2.4.7 Focus Visible | AA | Pass | 2px `#fde725` ring plus halo on every focusable, at 15.31:1 against the page. |
| **2.4.11 Focus Not Obscured (Min)** | AA | Pass | **New in 2.2.** See [Focus and stacking order](#focus-and-stacking-order). |
| 2.5.1 Pointer Gestures | A | Pass | Pinch-zoom and double-tap-drag both have single-pointer equivalents in the zoom buttons. No path-based gestures. |
| 2.5.2 Pointer Cancellation | A | Pass | All handlers fire on `click` / `change` / `input`, never on pointer-down. |
| 2.5.3 Label in Name | A | Pass | Visible label text is contained in the accessible name for every control. |
| 2.5.4 Motion Actuation | A | N/A | No motion actuation. |
| **2.5.7 Dragging Movements** | AA | Pass | **New in 2.2.** Map panning is a drag, and Leaflet ships no pan control. Four pan buttons were added, each performing one `panBy` per click with no dragging. Keyboard arrow-pan does **not** satisfy this criterion — it requires a single-*pointer* alternative — which is why buttons were necessary rather than sufficient keyboard support. |
| **2.5.8 Target Size (Minimum)** | AA | Pass, with a documented exception | **New in 2.2.** See [Target size](#target-size). |

### Understandable and Robust

| SC | Level | Result | Evidence |
|---|---|---|---|
| 3.1.1 Language of Page | A | Pass | `<html lang="en">`. |
| 3.1.2 Language of Parts | AA | N/A | Single language. |
| 3.2.1 On Focus | A | Pass | No context change on focus. |
| 3.2.2 On Input | A | Pass | Changing a control updates the map; no navigation or focus change. |
| 3.2.3 Consistent Navigation | AA | N/A | Single page. |
| 3.2.4 Consistent Identification | AA | Pass | — |
| **3.2.6 Consistent Help** | A | N/A | **New in 2.2.** No help mechanism, contact details or live chat is offered, so there is nothing whose placement could be inconsistent. |
| 3.3.1 Error Identification | A | N/A | No user input is validated. Update failures are reported in text with the archive state stated explicitly. |
| 3.3.2 Labels or Instructions | A | Pass | — |
| 3.3.3 Error Suggestion | AA | N/A | No input errors. |
| 3.3.4 Error Prevention | AA | N/A | No legal, financial or data-modifying user submission. |
| **3.3.7 Redundant Entry** | A | N/A | **New in 2.2.** No multi-step process and no information is ever re-entered. |
| **3.3.8 Accessible Authentication** | AA | N/A | **New in 2.2.** No authentication of any kind. |
| 4.1.2 Name, Role, Value | A | Pass | Every control has a name and role; state is exposed via `aria-valuetext`, `aria-current`, `aria-selected`, `aria-busy` and `aria-disabled`. |
| 4.1.3 Status Messages | AA | Pass | Two separate polite live regions — one for the timelapse, one for the basemap — so a basemap change cannot interrupt a timeline announcement. |

---

## Focus and stacking order

`2.4.11 Focus Not Obscured` was the hardest criterion here, and the way it was
missed is worth recording.

The application floats panels over a map that has its own control layer. Any
**fixed** stacking order fails this criterion somewhere: whatever sits on top
permanently obscures what is beneath it, so raising one layer to protect its
focusable content only relocates the failure. That is exactly what happened —
raising Leaflet's control corners to protect the attribution links put the scale
bar over the pan buttons and the credits over the filter panel, producing three
separate failures between 481 and 768 px wide, each measured at **zero visible
pixels** of the focused control.

Because the criterion concerns only the *focused* element, focus decides the
order:

```css
#info:focus-within,
#controls:focus-within,
#timeline:focus-within { z-index: 1400; }
.leaflet-top:focus-within,
.leaflet-bottom:focus-within { z-index: 1500 !important; }
```

Whichever container holds focus rises for as long as it holds it. Nothing moves
for a mouse user, and no breakpoint has to be hand-tuned. Leaflet's corners rank
highest so the pan and zoom buttons clear the scale bar, which is not focusable
and therefore never needs to win.

Verified per-pixel with `elementFromPoint` across the focused element's whole
box, at the sizes that previously failed:

| Viewport | Focusables | Fully obscured |
|---|---|---|
| 768×500 | 68 | 0 |
| 700×560 | 69 | 0 |
| 481×580 | 69 | 0 |

---

## Contrast over the map

`1.4.11` applies to the spray polygons themselves, not only to UI chrome, and it
is the reason the map looks the way it does.

The palette is Paul Tol's colourblind-safe qualitative set. A palette chosen to
be distinguishable under three kinds of colour vision deficiency **cannot also**
be chosen for luminance contrast against an arbitrary photograph; those are
different constraints on the same channel. Three of the six colours sit within
±0.05 relative luminance of satellite imagery's median, so no opacity setting can
create the required gap — the mark would rest on hue alone, which is precisely
what a CVD viewer cannot use.

Two mechanisms carry the criterion instead:

- **A casing stroke** — an opaque black or white outline drawn beneath the
  coloured one, in its own map pane. Its contrast is independent of the palette,
  so it holds whatever colour the operation is drawn in.
- **A per-basemap scrim** — a world-bounds rectangle between the finished
  basemap and the data, which makes the backdrop luminance predictable instead
  of "whatever happens to be under this polygon", and so makes the contrast
  computable rather than assumed.

Measured worst-case, against real tiles sampled across the county:

| Basemap | Casing | Scrim | Worst-case contrast | Backdrop detail retained |
|---|---|---|---|---|
| Dark | white | black 0.10 | 3.15:1 | 89% |
| Light | black | none | 7.57:1 | 100% |
| Terrain (light) | black | white 0.36 | 3.14:1 | 63% |
| Terrain (dark) | white | black 0.10 | 3.15:1 | 89% |
| Satellite | white | black 0.42 | 3.04:1 | 64% |

The scrim is deliberately the weaker of the two mechanisms. One strong enough to
carry `1.4.11` alone would need an alpha of 0.87–0.92 on three of the five, which
is a grey rectangle rather than a basemap.

**The age fade is load-bearing and was checked separately.** The fade is what the
timelapse *means*, so it is not enough for the freshest mark to pass. Contrast
between the freshest and oldest mark, worst colour per basemap: Terrain (light)
2.79:1, Terrain (dark) 2.27:1, Satellite 1.54:1 — all above the ~1.2:1
just-noticeable floor.

---

## Target size

`2.5.8` requires a 24×24 CSS px minimum. Two cases need explanation.

**Filter checkboxes and mode toggles** measure 14×14, but each sits inside a
clickable `<label>` of at least 24px height (79×24 and 130×24 for the toggles;
650×24 for filter rows), so the effective target meets the minimum. Nearest
neighbouring target centres are 44px and 75px away, so the Spacing exception
holds independently.

**Attribution links** are inline text within a sentence, covered by the *Inline*
exception.

### The calendar strip — a documented exception

The year strip renders one cell per day, 365 of them in a single row. At typical
widths each cell is about 3–5px wide, far below 24px, and this is **relied upon
as an exception rather than claimed as a pass.**

The exception invoked is **Equivalent**: *"the function can be achieved through a
different control on the same page that meets the target size"*. Every day the
strip can reach is reachable by conforming, full-size controls:

- the day scrubber, a native `<input type="range">` spanning the whole period,
  operable by click, drag or arrow keys, announcing the date it lands on;
- **step back** / **step forward** buttons, one day per activation;
- Page Up / Page Down on the scrubber, one week per press;
- the calendar grid's own keyboard interface — arrow keys by day, up/down by
  week, Page Up/Down by month, Home/End to the ends of the period.

This was **tested rather than assumed**: an auditor verified that every day in
the period is reachable through those controls without using the strip.

The strip is kept at this density deliberately. Showing a whole year at once is
what makes the seasonality legible — that roughly half of every calendar year has
no spraying at all — and expanding cells to 24px would require either a scrolling
strip or a month-grid, both of which destroy that. The strip is redundant with
controls that do conform, so no function is available only through it.

---

## Known limitations

1. **No screen-reader testing.** Structural verification only. California tests
   with JAWS and NVDA in Chrome; this has not been done, and until it is, claims
   about how content is *announced* are inferences from markup.
2. **`1.4.11` margins are thin by design.** Three basemaps clear 3:1 by less than
   0.2. They were solved against the worst pixel in a large sample rather than an
   average, but a basemap provider re-rendering its tiles could move them. If
   tiles change noticeably, the contrast solve should be re-run.
3. **Satellite is the weakest basemap for the age fade** at 1.54:1 between the
   freshest and oldest mark. Above the perceptual floor, but a reader comparing
   ages closely is better served by Dark or Light.
4. **`2.4.11` is satisfied by raising the focused container.** While a panel holds
   focus it may overlap the attribution bar. The credits remain present and
   reachable, and return to the top as soon as focus leaves.
5. **The calendar exception is an argued position**, not a mechanical pass. It is
   documented above so that a reviewer can disagree with it explicitly rather
   than discover it.
6. **Basemap tiles require a network connection.** Everything else, including the
   full archive and Leaflet itself, is local; only the backdrop needs the
   internet. With no connection the spray polygons draw on an empty background.

## Feedback

Accessibility problems with this map are bugs. Please open an issue on the
repository describing what you were trying to do, what happened, and the
assistive technology and browser you were using.
