# An Inherited Globe

1,273 places have been judged to hold "outstanding universal value" to humanity, and inscribed on the UNESCO World Heritage List since the 1972 Convention came into force. This interactive 3D globe maps every one of them. Sister map to [An Endangered Globe](https://github.com/tdemareuil/endangered-globe), and like it inspired by [Topi Tjukanov's Notable People](https://tjukanovt.github.io/notable-people): the map shows no city names — the world's geography is redrawn entirely through the names of what we inherited.

Where the endangered globe maps what we are about to lose, this one maps what was handed down.

## Table of Contents

- [Concept](#concept)
- [Categories Displayed](#categories-displayed)
- [Data Architecture](#data-architecture)
  - [Channel 1 — UNESCO World Heritage List export](#channel-1--unesco-world-heritage-list-export)
  - [Channel 2 — Wikidata (SPARQL API)](#channel-2--wikidata-sparql-api)
  - [Channel 3 — Wikipedia Pageviews (public REST API)](#channel-3--wikipedia-pageviews-public-rest-api)
- [Python Pipeline](#python-pipeline)
  - [Step 1 — Parsing & label points](#step-1--parsing--label-points)
  - [Step 2 — Popularity harvesting](#step-2--popularity-harvesting)
  - [Step 3 — Clean GeoJSON export](#step-3--clean-geojson-export)
- [Running the pipeline](#running-the-pipeline)
- [Web Interface](#web-interface)
- [Differences from the endangered globe](#differences-from-the-endangered-globe)
- [Reference](#reference)

---

## Concept

Two core mechanics drive the experience, identical to the sister project:

**Popularity-based prioritization.** The more Wikipedia pageviews a site has (e.g. the Taj Mahal, the Great Wall), the larger its name appears at low zoom. As you zoom in, space opens up and less-known properties emerge — a fluid "popcorn" effect. Sites are never clustered into bubbles; MapLibre's native symbol de-overlap hides the less popular name when two labels compete for the same patch of globe.

**Colors & filters.** Beneath each name, a small glowing dot pulses in a neon color tied to the property's category. The filter panel lets you isolate categories or UNESCO regions across the entire globe, and a search box narrows the map down to individual sites by name or country.

---

## Categories Displayed

| Key | Category | Dot color | Count |
|------|----------|-----------|-------|
| Cultural | Cultural property | Gold `#FFC24D` | 991 |
| Natural | Natural property | Neon green `#5CE68A` | 240 |
| Mixed | Mixed cultural & natural | Cyan `#4DD9FF` | 42 |
| Danger | On the List of World Heritage in Danger | Red `#FF4D5E` | 58 |

Danger is a flag, not a category: a property in danger is still Cultural, Natural or Mixed. On the globe the red dot **overrides** the category color, so the endangered part of the inheritance reads at a glance, and the popup names the underlying category on its own line.

Because the two are independent, so are the controls. The pipeline exports both: `color_key` (Cultural / Natural / Mixed / Danger) drives the dot color only, while `category` and the boolean `in_danger` drive the filters. The panel has three sections:

- **Category** — checkboxes on `category`, so ticking *Cultural* keeps an in-danger cultural property even though its dot is red.
- **World Heritage in Danger** — a single toggle labelled *In danger*. Off, it constrains nothing; on, it narrows to the 58 properties on the Danger List, composing with whatever category and region selection is already active. Its knob carries the same red the in-danger dots use on the globe, so the control names its own colour; the track stays inert, and only the knob moves.
- **Region** — checkboxes on the five UNESCO programme regions: Africa, Arab States, Asia and the Pacific, Europe and North America, Latin America and the Caribbean.

---

## Data Architecture

The project combines UNESCO and Wikimedia data through three technical channels in Python:

```
[ UNESCO WHC export ]          [ Wikidata ]            [ Wikipedia API ]
      (.CSV)                  (SPARQL query)           (REST Pageviews)
         │                          │                         │
 1. Names, category,         2. Site ID → Article       3. Traffic volume
    danger, region,             title mapping              over 12 months
    coordinates, ALL photos
```

Only channel 1 is mandatory: it already contains coordinates, categories and every photo the globe shows. Channels 2–3 exist purely to size the labels — **no image is ever fetched from Wikimedia.**

### Channel 1 — UNESCO World Heritage List export

What we take: English name and short description, category (Cultural / Natural / Mixed), World Heritage in Danger status and the year it was listed, inscription year, inscription criteria, area in hectares, states parties and ISO codes, UNESCO region, transboundary flag, official coordinates, component coordinates for serial properties, and the property's photo gallery with author and copyright.

The source file is the official list export published at [data.unesco.org](https://data.unesco.org/explore/assets/whc001/export/), committed here as `data/whc001.csv` (1,273 rows × 54 columns, downloaded 16 September 2026). It ships every field in six UN languages; the pipeline keeps the English block and the structured fields, and drops the other five.

Notable properties of the source data:

- **Coordinates.** 1,244 of the 1,273 rows carry a `Coordonnées` field (`lat, lon`). The 29 that don't are almost all inscribed at the most recent session; 28 of them are recovered from their `Components` field, which publishes one lat/lon per component. One property — *Funerary and memory sites of the First World War (Western Front)* — has neither, and is the only row the pipeline drops.
- **Serial properties.** `Components Count` runs from 0 to 758 (*Frontiers of the Roman Empire*). See [Step 1](#step-1--parsing--label-points) for how those become label points.
- **Photos.** 1,261 rows have a `Main Image`; the `Images` gallery averages 20 photos per property (max 145), 25,497 URLs in total. All are served from `whc.unesco.org/document/<id>`. Every one of them is exported and shown as a popup slide; the 12 rows with no `Main Image` have no gallery either, so those properties appear on the globe without a photo.
- **Encoding.** The file is UTF-8 with BOM, and the text fields contain occasional inline HTML (`<em>`, `<p>`) left by the UNESCO CMS. Both are handled in the pipeline.

The CSV is a complete snapshot of the List, so unlike the IUCN pipeline there is no API token, no rate limit, and no spatial download to manage for this channel. To refresh the map, download a newer export, replace `data/whc001.csv`, and re-run the notebook.

### Channel 2 — Wikidata (SPARQL API)

What we take: the Wikipedia article behind each property, and nothing else. Wikidata stores the UNESCO site ID as property [`P757`](https://www.wikidata.org/wiki/Property:P757), so one batched SPARQL query maps most of the List to a Wikidata item and its Wikipedia sitelinks. This is the exact counterpart of the `P627` (IUCN taxon ID) lookup in the sister project — except that the `P18` image is deliberately **not** requested, since photos come from the export.

Wikipedia language priority is English, German, French, Spanish, Italian, Russian, Japanese, Chinese, Portuguese, Dutch, then a further 20 languages, then any remaining Wikipedia sitelink returned by Wikidata. The list is wider than the endangered globe's because the World Heritage List is globally distributed by construction.

`abstract.wikipedia.org` is excluded explicitly: it matches the generic `.wikipedia.org` domain filter but is not a Wikipedia edition and has no pageview history.

**Article resolution fallback chain** — for sites not resolved by the initial `P757` batch query (mostly properties inscribed at the latest session, not yet in Wikidata):

1. **Wikidata entity search** (`wbsearchentities`) on each variant of the English name, then a QID-based SPARQL query for the sitelinks.
2. **Wikipedia direct title lookup**, resolving redirects, on each name variant.

Name variants matter here because UNESCO inscription names are written as titles, not as article names. The pipeline generates, in order: the full name; the name without a trailing parenthesised qualifier; the head of the name before an em-dash or colon subtitle; the name without a leading `The `; and the name without UNESCO title prefixes such as `Historic Centre of ` or `Old City of `.

Anything the chain still misses can be pinned by hand in the notebook's `MANUAL_ARTICLES` cell.

### Channel 3 — Wikipedia Pageviews (public REST API)

What we take: the cultural popularity score. Given the article title from Wikidata, the API returns the total view count over the past 12 completed months. The query uses `user` (human traffic only), excluding bots and automated crawlers.

Pageviews are fetched once per unique article and then filled back onto every label point of the property, so a serial property with two label points costs one request rather than two.

An optional [Wikimedia API token](https://api.wikimedia.org/) in the git-ignored `data/secrets/wikimedia_token.txt` raises the rate limit from 500 to 5,000 req/hour. The pageviews endpoint is IP-limited and does not honour the token, so that stage stays paced at roughly one request per second either way.

---

## Python Pipeline

All processing runs locally and produces a single lightweight GeoJSON file. Nothing heavy is left for the browser.

The notebook is intentionally kept as an orchestration layer; reusable helpers live in `scripts/pipeline_helpers.py`. There is no spatial pre-cleaning script and no geopandas/shapely dependency: UNESCO publishes point coordinates directly, so the entire spatial machinery of the sister project is unnecessary here.

### Step 1 — Parsing & label points

- Read the export, coerce the structured columns, and strip the CMS's inline HTML from names and descriptions.
- Derive `color_key` from category and danger status, and `unesco_url` from the site ID.
- Derive a `short_label` for the globe: UNESCO names run long (*Memphis and its Necropolis – the Pyramid Fields from Giza to Dahshur*), so names over `SHORT_LABEL_MAX_CHARS` (42) are cut at an em-dash, colon or comma boundary, then ellipsised. The full official name stays in the popup and in search. Individual labels can be overridden by hand in the notebook's last cell.
- Compute one or more **label points** per property:
  - no components → the official site coordinates;
  - components → they are clustered by great-circle distance with `COMPONENT_CLUSTER_BUFFER_KM` (250 km, single-linkage). The largest cluster always gets a point, snapped back to the official coordinates when those fall inside it. Secondary clusters get a point only if they hold at least `SECONDARY_CLUSTER_MIN_SHARE` (25%) of the components, capped by `MAX_LABEL_POINTS_PER_SITE`.
  - Default is one label point per property. Set `MAX_LABEL_POINTS_PER_SITE = 2` and `SHOW_MAIN_POINT_ONLY = false` in `index.html` to let big secondary clusters carry a second label; 35 properties qualify.
- Nudge apart label points that land on identical coordinates (`JITTER_DUPLICATE_POINTS`), so MapLibre's de-overlap doesn't permanently hide one of two adjacent inscriptions.

With the defaults, all 1,273 properties produce 1,273 label points: 1,218 on the published coordinates, 54 on a computed component centroid, and 1 on a hard-coded coordinate.

### Step 2 — Popularity harvesting

For each property:

1. Resolve a Wikipedia article via the fallback chain described in Channel 2 above.
2. Query the Wikimedia Pageviews API for the 12-month view count.
3. Take `Main Image` as the primary photo and **every** URL in the `Images` gallery as further popup slides. `MAX_EXTRA_IMAGES = None` keeps them all; set it to an integer to cap the slideshow and shrink the exported file.
4. Carry `Main Image Author`, `Main Image Copyright` and `Main Image Caption EN` through as `image_author`, `image_copyright` and `image_caption`. All three describe the main photo only — the export publishes no per-photo metadata for the gallery — so they are cleared for the 12 properties with no main photo.
5. Store `image_url`, `extra_image_urls`, `image_count` and the popularity score.

Every external call is cached on disk under `data/cache/` (git-ignored), keyed by article or site, so an interrupted run resumes instead of restarting. Checkpoints after parsing, after Wikidata and after pageviews let any stage be re-entered independently.

Image sources, in slide order — all from the export, none fetched online:

1. UNESCO `Main Image` for the property, with its author, copyright and caption
2. Every remaining photo in its UNESCO `Images` gallery

Descriptions are exported in full (`DESCRIPTION_MAX_CHARS = None`), averaging 585 characters and running to 1,943 for the longest, because the popup gives them a face of their own rather than a few lines under the facts.

> **On hotlinking.** UNESCO serves its photos from `whc.unesco.org/document/<id>` behind a bot challenge: scripted requests get a 403, so the notebook cannot verify them, but they load normally in a browser — verified in the globe. The popup still sends `referrerpolicy="no-referrer"` and collapses the whole image block if a photo fails, so the card degrades to text rather than showing a grey box.

### Step 3 — Clean GeoJSON export

The notebook produces `sites.geojson`, a list of GeoJSON Point features (~1.6 MB for the full List). A property appears more than once only when `MAX_LABEL_POINTS_PER_SITE > 1` and its components form several large clusters:

```json
{
  "type": "Feature",
  "geometry": { "type": "Point", "coordinates": [78.04222, 27.17417] },
  "properties": {
    "site_id": 252,
    "label": "Taj Mahal",
    "states": "India",
    "iso_codes": "IN",
    "region": "Asia and the Pacific",
    "category": "Cultural",
    "color_key": "Cultural",
    "in_danger": false,
    "date_inscribed": 1983,
    "criteria": "(i)",
    "area_hectares": 0.0,
    "short_description": "An immense mausoleum of white marble, built in Agra between 1631 and 1648 by order of the Mughal emperor Shah Jahan in memory of his favourite wife, the Taj Mahal is the jewel of Muslim art in India and one of the universally admired masterpieces of the world's heritage.",
    "component_count": 1,
    "unesco_url": "https://whc.unesco.org/en/list/252",
    "image_url": "https://whc.unesco.org/document/109419",
    "extra_image_urls": "https://whc.unesco.org/document/109420, …",
    "image_count": 58,
    "image_author": "M & G Therin-Weise",
    "image_copyright": "M & G Therin-Weise",
    "image_caption": "Taj Mahal",
    "image_source": "UNESCO",
    "wiki_title": "Taj Mahal",
    "wiki_language": "en",
    "wiki_project": "en.wikipedia.org",
    "wiki_url": "https://en.wikipedia.org/wiki/Taj_Mahal",
    "wikidata_url": "https://www.wikidata.org/entity/Q9141",
    "popularity": 4200000,
    "label_rank": 1,
    "label_count": 1,
    "point_source": "site_coordinates",
    "cluster_component_count": 1,
    "cluster_share": 1.0
  }
}
```

`short_label` is present only when the official name is too long for the globe; fields with no value (`area_hectares` when the export reports 0, `wiki_title` when no article resolved) are omitted rather than exported as null.

For dataset-level credit, use:

```text
Site data © UNESCO World Heritage Centre, official World Heritage List export, downloaded 16 September 2026. https://data.unesco.org/explore/assets/whc001/export/
Photographs © their respective authors, as credited per property.
```

If you refresh the export, update `WHC_EXPORT_DATE` in the notebook's configuration cell and the credit line at the foot of the Method panel in `index.html`.

---

## Running the pipeline

```bash
pip install pandas requests tqdm ipywidgets matplotlib jupyterlab
jupyter lab pipeline.ipynb
```

Run the cells top to bottom. The configuration cell holds every knob; the four `RUN_*` flags gate the network stages so they can be skipped or re-run independently:

| Flag | Stage | Cost |
|---|---|---|
| `RUN_WIKIDATA_BATCH` | `P757` SPARQL lookup | a few batched queries, ~1 min |
| `RUN_NAME_FALLBACK` | name-based resolution for the rest | one or more lookups per unresolved site |
| `RUN_PAGEVIEWS` | 12-month view counts | ~1 req/s per unique article (~20 min for the full List) |

None of them fetch images.

A full cold run takes roughly 25–40 minutes. Re-runs are near-instant: everything is cached under `data/cache/`.

The notebook ends with quality-check cells — popularity distribution, coverage by category and region, the least-viewed sites, and labels still long enough to crowd the globe.

Preview locally with any static server:

```bash
python3 -m http.server 8000
```

---

## Web Interface

### Stack (100% free, open-source)

| Role | Tool |
|---|---|
| 3D Globe engine | [MapLibre GL JS v5](https://maplibre.org/) — WebGL, native globe projection |
| Base map | CartoDB Dark Matter (no labels) — dark, label-free tiles |
| Starfield | [maplibre-gl-starfield](https://github.com/markmclaren/maplibre-gl-starfield) — custom celestial-vault motion |
| Hosting | GitHub Pages (static, no server needed) |

### Visual atmosphere

**Dark space background.** A custom starfield (600 stars) is rendered in a dedicated SVG layer behind the WebGL canvas. The stars move as a single curved celestial vault when the globe rotates, with a slight center-based rotation to avoid flat sliding.

**Styled globe.** CartoDB Dark Matter provides the label-free vector geometry, while the page overrides land, water, and boundary colors. The globe palette is currently identical to the sister project's — ocean `#17106F`, land `#3D38F0`, boundaries `#9F83EF`, minor lines `#5C51C8` — and is a work in progress. What is specific to this map: bold link text in the panel is violet `#D9A2FF`, and the category dot colors are those in the table above.

**Thin label halos.** Site labels use a very light text halo so names stay legible without a heavy outline.

### Rendering mechanics

**No clustering.** Bubble clustering destroys the intended effect. Instead, MapLibre's native symbol de-overlap:

```js
'text-allow-overlap': false,
'symbol-sort-key': ['*', -1, ['to-number', ['get', 'popularity'], 0]]
```

The negative sort key gives more popular sites placement priority, so the GPU hides less-known properties when a more popular one occupies the same geodesic area. This produces a smooth fade as you zoom.

Labels use `short_label` when present and wrap at `text-max-width: 8` ems, so a long inscription name becomes two or three short lines rather than one banner across a continent.

**Neon dots.** Below each text label, a `circle` layer with `'circle-blur': 0.4`, colored by `color_key` (see table above). At low zoom, the Earth appears covered in a glowing swarm of gold, green and red fireflies before individual names become legible.

**Glassmorphism UI.** The filter panel floats over the map in a blurred card: category checkboxes, then the Danger List toggle in its own section, then region checkboxes, with a Method view behind a "here" link.

**Zoom on a random site.** A button under the filters picks a random property among those the current filters keep — restricted to sites that have an image — then flies the globe to it and opens its popup.

**Search sites.** A second button turns into a text field in place, matching on site name *and* states parties, so `japan` finds every Japanese property. Typing pauses for 500 ms, then the globe filters to the matches and a scrollable list of results opens above the field; clicking one flies to that site. Matching is accent-insensitive — `angkor`, `Ḥatra`, `Sidi Bou Saïd` all behave. The list shows 30 results at a time, extended 30 more per click on its footer.

Both are composed with the other controls rather than overriding them, so a search narrows whatever category, danger and region selection is already active.

**Popup — a two-sided card.** The front holds the facts, kept deliberately short: the photo, the site name linking to its UNESCO record, category and inscription year, states parties, area, annual Wikipedia views, and source links to UNESCO, Wikipedia and Wikidata. Region, inscription criteria and component count stay in `sites.geojson` — `region` still drives the region filter — but are not printed on the card.

A small circled **i** beside the title flips the card on its vertical axis to reveal the property's full UNESCO description, with a `←` button to flip back. The card's height animates with the rotation so each face is sized to its own content, and a long description scrolls inside the back face rather than stretching the card.

The source row is `UNESCO | Wikipedia | Wikidata` — records, not image files. The Wikipedia slot is always rendered: a real link once the pipeline resolves the article, and a dimmed, non-clickable placeholder until then, so the row does not reflow when popularity data lands. The photo credit sits below it as plain text, `Photo: <author> · © <copyright>`, collapsed to a single `© <name>` when the export repeats the same name in both columns — which it usually does. `Main Image Caption EN` is not shown by default; it fades in over the bottom of the photo when the pointer is **moved over** the photo. Deliberately not CSS `:hover`: MapLibre anchors a popup below the clicked point whenever there is room above it, which puts the photo directly under the cursor, so `:hover` matched the instant the card opened. A class set on real `mousemove` means a stationary cursor never reveals it. `:focus-within` is avoided for the same reason — the slide arrows live inside the image wrap and would pin the caption open after a click. Captions belong to the main photo only, so they disappear when you page into the gallery.

Galleries run from 1 to 58 photos (145 in the raw export for one property), so the slideshow shows a `3 / 58` counter rather than a row of dots past twelve slides, and preloads only the neighbouring photo instead of the whole gallery.

**Locate me.** A crosshair button in the bottom-right control stack flies the globe to the visitor's approximate location. It resolves the position from the visitor's **IP address**, not the browser Geolocation API — deliberately, so a single click works with no permission prompt. The trade-off is accuracy: IP geolocation is city-level at best, providers routinely disagree by a few hundred kilometres, and VPN users land at their exit node. The landing zoom is therefore regional (5) rather than city-level.

Two providers are queried in order — [ipwho.is](https://ipwho.is/), then [geojs.io](https://get.geojs.io/) — each with a 6 s timeout, since privacy extensions such as uBlock Origin block these endpoints outright. The result is cached for the session. The lookup fires **on click only**, never on page load, so no visitor is geolocated merely for opening the map.

All camera moves share one `FLY_DURATION_MS` (4600 ms) so the globe always travels at the same pace.

---

## Differences from the endangered globe

Everything that made the sister project work is kept — starfield, de-overlap, popularity sizing, glassmorphism panel, search, random, locate, popup slideshow. What changed:

| | Endangered Globe | Inherited Globe |
|---|---|---|
| Subject | Threatened animal species | UNESCO World Heritage properties |
| Point source | Centroids computed from IUCN range polygons | Official UNESCO coordinates, clustered for serial properties |
| Color dimension | IUCN threat category (EW→NT) | Category, overridden by danger status |
| Second filter | Animal group | UNESCO region |
| Third filter | — | World Heritage in Danger toggle |
| Search fields | Common + scientific name | Site name + states parties |
| Images | Wikipedia / Wikidata / Commons / iNaturalist, fetched per taxon | UNESCO photo gallery, straight from the export, never fetched |
| Wikidata key | `P627` (IUCN taxon ID) | `P757` (World Heritage Site ID) |
| Heavy dependencies | geopandas, shapely, IUCN API token, ~70 GB of shapefiles | pandas + requests |
| Data source | 5 APIs + local spatial downloads | 1 committed CSV + 3 public APIs |

The globe label is shortened for display here (`short_label`) because UNESCO inscription names are sentences, where species names are nouns.

---

## Reference

- [UNESCO World Heritage List](https://whc.unesco.org/en/list/) and its [data export](https://data.unesco.org/explore/assets/whc001/export/)
- [List of World Heritage in Danger](https://whc.unesco.org/en/danger/)
- [An Endangered Globe](https://github.com/tdemareuil/endangered-globe) — the sister project this one derives from
- [Notable People by Topi Tjukanov](https://tjukanovt.github.io/notable-people) — visual and UX inspiration
- [MapLibre GL JS docs](https://maplibre.org/maplibre-gl-js/docs/)
- [Wikidata SPARQL endpoint](https://query.wikidata.org/) — property [`P757`](https://www.wikidata.org/wiki/Property:P757)
- [Wikimedia Pageviews API](https://wikitech.wikimedia.org/wiki/Analytics/AQS/Pageviews)
- [maplibre-gl-starfield plugin](https://github.com/markmclaren/maplibre-gl-starfield)
