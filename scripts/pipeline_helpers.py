#!/usr/bin/env python3
"""Helper functions for the inherited-globe notebook.

The notebook stays readable and focuses on orchestration. Reusable helpers for
parsing the UNESCO World Heritage List export, placing label points, resolving
Wikipedia articles, harvesting pageviews, and exporting GeoJSON live here.

Adapted from the sister project `endangered-globe`. The spatial machinery is gone:
UNESCO publishes one official coordinate pair per property (plus one per component
for serial properties), so there are no shapefiles to clean and no centroids to
compute from polygons.
"""

from __future__ import annotations

import html
import io
import json
import math
import os
import re
from concurrent import futures
import time
import unicodedata
import urllib.parse
from pathlib import Path

import pandas as pd
import requests
# tqdm.auto, not tqdm.notebook: the notebook bar needs ipywidgets and raises
# ImportError without it, where auto quietly falls back to the text bar.
from tqdm.auto import tqdm

# ── Progress-bar safety ───────────────────────────────────────────────────────
# tqdm registers an instance in a class-level WeakSet before it builds its display.
# tqdm.notebook's build raises ImportError when ipywidgets is missing, which leaves a
# half-built bar in that registry — held alive by the traceback IPython keeps — for the
# rest of the kernel's life. Every later tqdm.write() then refreshes it and dies with
# "AttributeError: 'tqdm_notebook' object has no attribute 'container'", in a cell that
# has nothing to do with progress bars. Two guards: drop those bars when this module is
# imported or reloaded, and never let a write take a run down with it.


def drop_broken_bars():
    """Remove progress bars that failed mid-construction. Returns how many."""
    registry = getattr(tqdm, "_instances", None)
    if not registry:
        return 0
    broken = [bar for bar in list(registry) if not hasattr(bar, "container")
              and type(bar).__module__.endswith("notebook")]
    for bar in broken:
        # close() short-circuits on a disabled bar, which keeps the garbage collector
        # from tripping over the same missing attributes in __del__.
        bar.disable = True
        try:
            registry.remove(bar)
        except KeyError:
            pass
    return len(broken)


def note(message):
    """tqdm.write(), guarded against a bar that failed mid-construction.

    Clearing first rather than catching afterwards: tqdm writes the message before it
    refreshes the other bars, so a write that raises has already printed, and a retry
    in the handler would print it twice.
    """
    drop_broken_bars()
    try:
        tqdm.write(message)
    except Exception:
        print(message)


drop_broken_bars()


# ── Runtime configuration. The notebook calls configure() after defining its knobs. ──
USER_AGENT = "InheritedGlobe/1.0 (https://github.com/tdemareuil/inherited-globe)"
WIKIMEDIA_TOKEN = ""  # Optional — raises rate limit from 500 to 5,000 req/hour
SLEEP_WIKI = 0.15       # Wikimedia action/REST API (token-authenticated, ~6.6 req/s)
SLEEP_PAGEVIEWS = 1.0   # AQS pageviews endpoint (IP-based limit, token not honoured)
SLEEP_SPARQL = 1.0      # Wikidata Query Service: be gentle
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
PAGEVIEWS_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
PAGEVIEWS_AGENT = "user"  # "user" = human only, "all-agents" = humans + bots
UNESCO_SITE_URL = "https://whc.unesco.org/en/list/{site_id}"
START = ""
END = ""

# Language priority for the Wikipedia article behind each site's pageview count.
# UNESCO properties are global, so the list is wider than a purely European one.
WIKIPEDIA_LANGUAGE_PRIORITY = [
    "en", "de", "fr", "es", "it", "ru", "ja", "zh", "pt", "nl",
    "pl", "ar", "uk", "sv", "tr", "ko", "fa", "cs", "id", "vi",
    "hi", "he", "fi", "da", "no", "hu", "ro", "el", "ca", "th",
]
WIKIPEDIA_LANGUAGE_RANK = {lang: rank for rank, lang in enumerate(WIKIPEDIA_LANGUAGE_PRIORITY)}

WIKIDATA_FIELDS = [
    "wiki_title",
    "wiki_language",
    "wiki_project",
    "wiki_url",
    "wikidata_url",
]

# UNESCO's five programme regions, as written in the official export.
UNESCO_REGIONS = [
    "Africa",
    "Arab States",
    "Asia and the Pacific",
    "Europe and North America",
    "Latin America and the Caribbean",
]

# Category → color key used by index.html. Sites on the List of World Heritage in
# Danger take the "Danger" key instead, so the red dot overrides their category color.
CATEGORIES = ["Cultural", "Natural", "Mixed"]
DANGER_COLOR_KEY = "Danger"

# UNESCO Global Geoparks come from a separate programme and a separate export
# (data/eg0001.csv), so they are not a World Heritage category: they carry their own
# category and color key, and are never in danger — the Danger list is a World
# Heritage instrument only.
GEOPARK_CATEGORY = "Geopark"

# Wikidata's item for the designation itself, used to find the geopark items.
GEOPARK_DESIGNATION_QID = "Q53444003"

# The geopark export has no region column, but its `Internal ID` is prefixed with the
# programme region (EUFR10, ASCN01, NACA03...). Canada and New Zealand follow the
# World Heritage convention of folding into the neighbouring region, so the five
# regions in the globe's filter stay the same for both datasets.
GEOPARK_REGION_PREFIXES = {
    "AF": "Africa",
    "AR": "Arab States",
    "AS": "Asia and the Pacific",
    "OC": "Asia and the Pacific",
    "EU": "Europe and North America",
    "NA": "Europe and North America",
    "LA": "Latin America and the Caribbean",
}

# The geopark export names countries by ISO 3166-1 alpha-2 code only, where the World
# Heritage export ships both the codes and the written names. Covers every code the
# geopark export uses.
COUNTRY_NAMES = {
    "AT": "Austria", "BE": "Belgium", "BR": "Brazil", "CA": "Canada",
    "CL": "Chile", "CN": "China", "CY": "Cyprus", "CZ": "Czechia",
    "DE": "Germany", "DK": "Denmark", "EC": "Ecuador", "ES": "Spain",
    "FI": "Finland", "FR": "France", "GB": "United Kingdom", "GR": "Greece",
    "HR": "Croatia", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland",
    "IR": "Iran", "IS": "Iceland", "IT": "Italy", "JP": "Japan",
    "KP": "North Korea", "KR": "Republic of Korea", "LU": "Luxembourg",
    "MA": "Morocco", "MX": "Mexico", "MY": "Malaysia", "NI": "Nicaragua",
    "NL": "Netherlands", "NO": "Norway", "NZ": "New Zealand", "PE": "Peru",
    "PH": "Philippines", "PL": "Poland", "PT": "Portugal", "RO": "Romania",
    "RS": "Serbia", "RU": "Russian Federation", "SA": "Saudi Arabia",
    "SE": "Sweden", "SI": "Slovenia", "SK": "Slovakia", "TH": "Thailand",
    "TN": "Tunisia", "TR": "Türkiye", "TZ": "United Republic of Tanzania",
    "UY": "Uruguay", "VN": "Viet Nam",
}


def configure(**kwargs):
    """Set runtime values supplied by the notebook configuration cell."""
    globals().update({key: value for key, value in kwargs.items() if value is not None})


def set_pageview_window(start, end):
    """Set the Wikimedia pageview window used by get_pageviews()."""
    configure(START=start, END=end)


# api.wikimedia.org hands you the whole credential block when you create a personal API
# token, and the obvious thing to do is save it as-is:
#
#   {client_application_key: 7f3…, client_application_secret: 9ab…, access_token: eyJ0…}
#
# Only the access token belongs in an Authorization header, and that block is neither JSON
# (the keys are unquoted) nor a Python literal, so it is matched directly. A file holding
# nothing but the token still works, which is the other way people save it.
_ACCESS_TOKEN_RE = re.compile(
    r"""["']?access_token["']?\s*[:=]\s*["']?([A-Za-z0-9._\-]+)["']?""")


def read_local_secret(path):
    """Read a local secret file ignored by git, returning an empty string if absent.

    Accepts a bare token, a JSON object, or the credential block api.wikimedia.org
    displays; in the latter two cases the `access_token` field is what comes back.
    """
    path = Path(path)
    if not path.exists():
        return ""
    raw = path.read_text().strip()
    if not raw:
        return ""
    if raw.startswith(("{", "[")) or "access_token" in raw:
        match = _ACCESS_TOKEN_RE.search(raw)
        if match:
            return match.group(1)
        print(f"  [secret] {path} looks structured but has no access_token field")
        return ""
    return raw


def clean_str(value):
    """Return a stripped string, or '' for None/NaN/non-string values."""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in {"nan", "none", "<na>"} else s


def wikimedia_headers():
    """Build request headers for Wikimedia APIs, injecting Bearer token when configured."""
    headers = {"User-Agent": USER_AGENT}
    token = (WIKIMEDIA_TOKEN or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def wikimedia_retry_after(response, default=10):
    """Return seconds to wait from a Wikimedia 429 Retry-After header, or default."""
    header = response.headers.get("Retry-After", "")
    if header:
        try:
            return int(header)
        except ValueError:
            pass
    return default


def pick_path(obj, *paths):
    """Return the first non-empty value found at any of the given nested key paths."""
    for path in paths:
        current = obj
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current:
            return current
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 1. UNESCO export parsing
# ══════════════════════════════════════════════════════════════════════════════

def strip_tags(value):
    """Drop the occasional inline HTML (<em>, <p>, <br>) the UNESCO CMS leaves in text.

    Both exports also leave HTML entities behind — `&amp;` in the World Heritage names,
    numeric ones such as `&#160;` in the geopark introductions — so the text is unescaped
    after the tags come out.
    """
    text = clean_str(value)
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# Both exports concatenate a record's paragraphs into one field and lose the space at the
# join, so a sentence ends flush against the next one: "the Japanese archipelago.At the
# Date Museum". 226 of the 241 geopark introductions are affected, and one World Heritage
# description. The guard is the character before the stop: it has to be lowercase, a digit
# or a closing quote, which leaves initialisms ("U.S.A"), decimals ("1.5") and ellipses
# alone.
_GLUED_SENTENCE_RE = re.compile(r'''([a-z0-9)\u201d\u2019"'])([.!?])([A-Z\u201c])''')


def description_text(value):
    """Clean a description or introduction for display: tags out, sentence spacing back."""
    return _GLUED_SENTENCE_RE.sub(r"\1\2 \3", strip_tags(value))


def parse_coordinates(value):
    """Parse the export's 'Coordonnées' field ('lat, lon') into a (lat, lon) tuple.

    Returns (None, None) when the field is empty or unparseable. Sites inscribed at the
    most recent session sometimes ship without it, and fall back to their components.
    """
    text = clean_str(value)
    if not text:
        return (None, None)
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if len(parts) < 2:
        return (None, None)
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return (None, None)
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return (None, None)
    return (lat, lon)


# The export writes components as a pseudo-JSON list with unquoted keys:
#   {name: Hanklit, ref: 1758-001, latitude: 56.895649, longitude: 8.751962}, {...}
_COMPONENT_RE = re.compile(
    r"\{\s*name:\s*(?P<name>.*?),\s*ref:\s*(?P<ref>.*?),\s*"
    r"latitude:\s*(?P<lat>-?[\d.]+)\s*,\s*longitude:\s*(?P<lon>-?[\d.]+)\s*\}",
    re.S,
)


def parse_components(value):
    """Parse the export's 'Components' field into a list of {name, ref, lat, lon} dicts."""
    text = clean_str(value)
    if not text:
        return []
    components = []
    for match in _COMPONENT_RE.finditer(text):
        try:
            lat, lon = float(match.group("lat")), float(match.group("lon"))
        except ValueError:
            continue
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            continue
        components.append(
            {
                "name": match.group("name").strip(),
                "ref": match.group("ref").strip(),
                "lat": lat,
                "lon": lon,
            }
        )
    return components


def parse_url_list(value):
    """Split the export's comma-separated image/video URL fields into a clean list."""
    text = clean_str(value)
    if not text:
        return []
    seen, urls = set(), []
    for candidate in re.split(r"[,\s]+", text):
        candidate = candidate.strip().rstrip(",")
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        candidate = re.sub(r"^http://", "https://", candidate, flags=re.I)
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
    return urls


def unesco_site_url(site_id):
    """Build the canonical whc.unesco.org page URL for a site ID."""
    sid = clean_str(site_id)
    if not sid.isdigit():
        return None
    return UNESCO_SITE_URL.format(site_id=sid)


# Every name in the geopark export ends in the programme's own words. They are worth
# keeping in the popup title and in what the search box matches, but not on the globe
# (241 identical tails) and not in a Wikidata search box.
_GEOPARK_LABEL_SUFFIX = re.compile(
    r"\s*[-–—,]?\s*(?:UNESCO\s+)?Global\s+Geopark\s*$", re.I)


def geopark_plain_name(name):
    """Strip the trailing 'UNESCO Global Geopark' from a geopark name."""
    base = strip_tags(name)
    stripped = _GEOPARK_LABEL_SUFFIX.sub("", base).strip(" ,;:-–—")
    return stripped or base


def geopark_short_label(name, max_chars=42):
    """On-globe label for a geopark: the name without the programme words, shortened.

    Unlike short_label(), this always returns a string: the programme suffix has to come
    off every geopark label, so there is never a case where the full name will do.
    """
    plain = geopark_plain_name(name)
    return short_label(plain, max_chars) or plain


def geopark_region(internal_id):
    """Map a geopark `Internal ID` (EUFR10, ASCN01) to its UNESCO programme region."""
    code = clean_str(internal_id).upper()
    return GEOPARK_REGION_PREFIXES.get(code[:2])


def country_names(iso_codes):
    """Turn the geopark export's "AT,SI" country codes into "Austria, Slovenia"."""
    codes = [c.strip().upper() for c in clean_str(iso_codes).split(",") if c.strip()]
    names = [COUNTRY_NAMES.get(code, code) for code in codes]
    return ", ".join(dict.fromkeys(names))


def category_color_key(category, in_danger):
    """Return the color key used by index.html: danger overrides the category color."""
    if in_danger:
        return DANGER_COLOR_KEY
    category = clean_str(category).title()
    return category if category in CATEGORIES else "Cultural"


# Trailing clauses UNESCO uses to qualify a property, dropped from the globe label
# because the full inscription name is often a sentence. The full name stays in the popup.
def truncate_text(text, max_chars):
    """Cut a description to max_chars on a word boundary. None = keep it whole."""
    text = clean_str(text)
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "\u2026"


_LABEL_SPLIT_TOKENS = [" – ", " — ", " -- ", ": ", " and Associated ", " and its Associated "]


def short_label(name, max_chars=42):
    """Shorten an inscription name for on-globe display, keeping the popup name intact.

    UNESCO names run long ('Memphis and its Necropolis – the Pyramid Fields from Giza to
    Dahshur'). The globe reads better with the head of the name; the popup keeps the
    official one. Returns None when the name is already short enough.
    """
    full = strip_tags(name)
    if not full or len(full) <= max_chars:
        return None

    head = full
    for token in _LABEL_SPLIT_TOKENS:
        if token in head:
            candidate = head.split(token, 1)[0].strip()
            if len(candidate) >= 8:
                head = candidate
                break
    if len(head) <= max_chars:
        return head if head != full else None

    # Still long: cut at the last comma, then at the last word boundary.
    if "," in head[:max_chars + 12]:
        candidate = head[: max_chars + 12].rsplit(",", 1)[0].strip()
        if len(candidate) >= 8:
            return candidate
    cut = head[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(" ,;:-–—") + "…"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Label points
# ══════════════════════════════════════════════════════════════════════════════

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def mean_position(points):
    """Average a list of (lat, lon) points on the sphere, so antimeridian groups behave."""
    if not points:
        return (None, None)
    xs = ys = zs = 0.0
    for lat, lon in points:
        phi, lam = math.radians(lat), math.radians(lon)
        xs += math.cos(phi) * math.cos(lam)
        ys += math.cos(phi) * math.sin(lam)
        zs += math.sin(phi)
    n = len(points)
    xs, ys, zs = xs / n, ys / n, zs / n
    hyp = math.hypot(xs, ys)
    if hyp < 1e-12 and abs(zs) < 1e-12:
        return points[0]
    return (math.degrees(math.atan2(zs, hyp)), math.degrees(math.atan2(ys, xs)))


def cluster_points(points, buffer_km):
    """Single-linkage clustering of (lat, lon) points by great-circle distance.

    Mirrors the range-cluster step of the sister project: components of one serial
    property that sit close together become a single label point.
    """
    if not points:
        return []
    remaining = list(range(len(points)))
    clusters = []
    while remaining:
        seed = remaining.pop(0)
        cluster = [seed]
        changed = True
        while changed:
            changed = False
            for idx in list(remaining):
                lat, lon = points[idx]
                if any(haversine_km(lat, lon, *points[member]) <= buffer_km for member in cluster):
                    cluster.append(idx)
                    remaining.remove(idx)
                    changed = True
        clusters.append(cluster)
    clusters.sort(key=len, reverse=True)
    return clusters


def site_label_points(main_lat, main_lon, components, buffer_km, max_points,
                      secondary_min_share):
    """Return the label points for one property, most representative first.

    - No components: the official site coordinates are the only point.
    - Components: they are clustered by distance. The largest cluster always gets a
      point — the centroid (spherical mean) of its components, snapped to the official
      coordinates when those fall inside it. Secondary clusters get a point only if
      they hold at least `secondary_min_share` of the components, capped at `max_points`.

    A property with no published coordinates is therefore placed on a computed centroid,
    never dropped, as long as it publishes at least one component.
    """
    has_main = main_lat is not None and main_lon is not None
    if not components:
        if not has_main:
            return []
        return [
            {
                "lat": main_lat,
                "lon": main_lon,
                "point_source": "site_coordinates",
                "cluster_component_count": 0,
                "cluster_share": 1.0,
            }
        ]

    coords = [(c["lat"], c["lon"]) for c in components]
    clusters = cluster_points(coords, buffer_km)
    total = len(coords)
    label_points = []
    for rank, cluster in enumerate(clusters, start=1):
        share = len(cluster) / total
        if rank > 1 and (share < secondary_min_share or len(label_points) >= max_points):
            continue
        members = [coords[i] for i in cluster]
        if rank == 1 and has_main and any(
            haversine_km(main_lat, main_lon, lat, lon) <= buffer_km for lat, lon in members
        ):
            lat, lon = main_lat, main_lon
            source = "site_coordinates"
        else:
            lat, lon = mean_position(members)
            source = "component_centroid"
        label_points.append(
            {
                "lat": lat,
                "lon": lon,
                "point_source": source,
                "cluster_component_count": len(cluster),
                "cluster_share": round(share, 4),
            }
        )
        if len(label_points) >= max_points:
            break

    if not label_points and has_main:
        return [
            {
                "lat": main_lat,
                "lon": main_lon,
                "point_source": "site_coordinates",
                "cluster_component_count": 0,
                "cluster_share": 1.0,
            }
        ]
    return label_points


def jitter_duplicate_points(df, lat_col="lat", lon_col="lon", key_col="site_id",
                            offset_km=3.0):
    """Nudge apart label points that land on the exact same coordinates.

    Two properties occasionally share a published coordinate pair (adjacent inscriptions,
    or a rounded value). MapLibre's de-overlap would then permanently hide one of the two.
    """
    df = df.copy()
    grouped = df.groupby([df[lat_col].round(6), df[lon_col].round(6)])
    moved = 0
    for _, idx in grouped.groups.items():
        rows = list(idx)
        if len(rows) < 2:
            continue
        for offset, row in enumerate(rows[1:], start=1):
            lat = float(df.at[row, lat_col])
            angle = 2 * math.pi * offset / max(1, len(rows) - 1)
            dlat = (offset_km / 111.32) * math.cos(angle)
            dlon = (offset_km / (111.32 * max(0.15, math.cos(math.radians(lat))))) * math.sin(angle)
            df.at[row, lat_col] = max(-89.5, min(89.5, lat + dlat))
            df.at[row, lon_col] = ((float(df.at[row, lon_col]) + dlon + 180) % 360) - 180
            moved += 1
    if moved:
        print(f"  [jitter] moved {moved} overlapping label points by ~{offset_km} km")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 3. Wikidata resolution
# ══════════════════════════════════════════════════════════════════════════════

def article_rank(article_lang):
    """Rank Wikipedia languages; any unlisted language remains usable after the preferred list."""
    return WIKIPEDIA_LANGUAGE_RANK.get(str(article_lang), len(WIKIPEDIA_LANGUAGE_PRIORITY))


# abstract.wikipedia.org is a Wikimedia project, not a Wikipedia edition, and has no
# pageview history — excluded explicitly rather than by the generic domain filter.
_SITELINK_BLOCK = 'FILTER(!CONTAINS(STR(?wiki_site), "abstract.wikipedia.org"))'


def build_sparql_query(whc_ids):
    """Batch SPARQL: resolve UNESCO World Heritage site IDs to sitelinks and images."""
    values = " ".join(f'"{i}"' for i in whc_ids)
    return f"""
SELECT ?whc_id ?item ?article ?article_lang ?wiki_project ?article_title WHERE {{
  VALUES ?whc_id {{ {values} }}
  ?item wdt:P757 ?whc_id .             # P757 = World Heritage Site ID
  ?article schema:about ?item ;
            schema:inLanguage ?article_lang ;
            schema:isPartOf ?wiki_site .
  FILTER(CONTAINS(STR(?wiki_site), ".wikipedia.org/"))
  {_SITELINK_BLOCK}
  BIND(REPLACE(STR(?wiki_site), "^https?://", "") AS ?wiki_project_slash)
  BIND(REPLACE(?wiki_project_slash, "/$", "") AS ?wiki_project)
  BIND(REPLACE(STR(?article), CONCAT("https://", ?wiki_project, "/wiki/"), "") AS ?article_title)
}}
"""


def build_sparql_qid_query(qids):
    """SPARQL to get Wikipedia sitelinks for a known list of Wikidata QIDs."""
    values = " ".join(f"wd:{qid}" for qid in qids)
    return f"""
SELECT ?item ?article ?article_lang ?wiki_project ?article_title WHERE {{
  VALUES ?item {{ {values} }}
  ?article schema:about ?item ;
            schema:inLanguage ?article_lang ;
            schema:isPartOf ?wiki_site .
  FILTER(CONTAINS(STR(?wiki_site), ".wikipedia.org/"))
  {_SITELINK_BLOCK}
  BIND(REPLACE(STR(?wiki_site), "^https?://", "") AS ?wiki_project_slash)
  BIND(REPLACE(?wiki_project_slash, "/$", "") AS ?wiki_project)
  BIND(REPLACE(STR(?article), CONCAT("https://", ?wiki_project, "/wiki/"), "") AS ?article_title)
}}
"""


def _entry_from_sparql_row(row, item_key="item"):
    """Build a wikidata_map entry dict from one SPARQL result row."""
    return {
        "wikidata_url": row[item_key]["value"].replace("http://", "https://", 1),
        "wiki_title": urllib.parse.unquote(row["article_title"]["value"]),
        "wiki_language": row["article_lang"]["value"],
        "wiki_project": row["wiki_project"]["value"],
        "wiki_url": row["article"]["value"],
        "wiki_rank": article_rank(row["article_lang"]["value"]),
        "wiki_lookup_source": "wikidata_P757",
    }


def _merge_entry(mapping, key, candidate):
    """Merge a candidate entry into mapping, keeping the best-ranked language."""
    existing = mapping.get(key)
    if existing is None:
        mapping[key] = candidate
        return
    if candidate["wiki_rank"] < existing["wiki_rank"]:
        existing.update({
            k: candidate[k]
            for k in ("wiki_title", "wiki_language", "wiki_project", "wiki_url", "wiki_rank")
        })


def _post_sparql(sparql, retries=4, timeout=90):
    """POST a SPARQL query, backing off on 429/5xx."""
    for attempt in range(retries):
        try:
            response = requests.post(
                WIKIDATA_ENDPOINT,
                data={"query": sparql},
                headers={
                    **wikimedia_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/sparql-results+json",
                },
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            if attempt == retries - 1:
                raise
            note(f"  [sparql] {exc} — retrying")
            time.sleep(2 ** attempt * 3)
            continue
        if response.status_code in (429, 500, 502, 503, 504):
            wait = wikimedia_retry_after(response, default=2 ** attempt * 5)
            note(f"  [sparql] HTTP {response.status_code} — waiting {wait}s")
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response.json()
    return None


def query_wikidata_batch(whc_ids, batch_size=300):
    """Run the P757 SPARQL lookup in batches to stay under the query size limit."""
    mapping = {}
    ids = [str(i) for i in whc_ids]
    for start in tqdm(range(0, len(ids), batch_size), desc="Wikidata batches"):
        batch = ids[start:start + batch_size]
        data = _post_sparql(build_sparql_query(batch))
        if not data:
            continue
        for row in data["results"]["bindings"]:
            _merge_entry(mapping, row["whc_id"]["value"], _entry_from_sparql_row(row))
        time.sleep(SLEEP_SPARQL)
    return mapping


def wikidata_entity_search(search_term, retries=3):
    """Search Wikidata by label and return the best entry with a Wikipedia sitelink."""
    for attempt in range(retries):
        try:
            response = requests.get(
                WIKIDATA_API_URL,
                params={
                    "action": "wbsearchentities",
                    "search": search_term[:250],
                    "language": "en",
                    "type": "item",
                    "format": "json",
                    "limit": 5,
                },
                headers=wikimedia_headers(),
                timeout=20,
            )
            if response.status_code == 429:
                wait = wikimedia_retry_after(response, default=2 ** attempt * 3)
                note(f"  [wbsearchentities] 429 — waiting {wait}s")
                time.sleep(wait)
                continue
            response.raise_for_status()
            break
        except requests.exceptions.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt * 2)
    else:
        return None

    qids = [hit["id"] for hit in response.json().get("search", [])]
    if not qids:
        return None
    data = _post_sparql(build_sparql_qid_query(qids), timeout=45)
    rows = (data or {}).get("results", {}).get("bindings", [])
    if not rows:
        return None
    best = min(rows, key=lambda row: article_rank(row["article_lang"]["value"]))
    entry = _entry_from_sparql_row(best)
    entry["wiki_lookup_source"] = "wikidata_entity_search"
    return entry


def wikipedia_direct_search(search_term, lang="en", retries=3):
    """Check whether a Wikipedia page exists for search_term, resolving redirects."""
    for attempt in range(retries):
        try:
            response = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "query", "titles": search_term, "redirects": True, "format": "json"},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            if response.status_code == 429:
                wait = wikimedia_retry_after(response, default=2 ** attempt * 3)
                note(f"  [wikipedia direct] 429 — waiting {wait}s")
                time.sleep(wait)
                continue
            response.raise_for_status()
            break
        except requests.exceptions.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt * 2)
    else:
        return None

    pages = response.json().get("query", {}).get("pages", {})
    for page_id, page in pages.items():
        if page_id == "-1":
            return None
        title = page.get("title", search_term)
        project = f"{lang}.wikipedia.org"
        return {
            "wiki_title": title,
            "wiki_language": lang,
            "wiki_project": project,
            "wiki_url": f"https://{project}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
            "wikidata_url": None,
            "wiki_rank": article_rank(lang),
            "wiki_lookup_source": f"wikipedia_direct_{lang}",
        }
    return None


def name_variants(name):
    """Return lookup candidates for a site name, from most to least specific.

    The export's English names carry qualifiers Wikipedia usually drops: a trailing
    country in parentheses, a leading 'The ', an em-dash subtitle.
    """
    base = strip_tags(name)
    if not base:
        return []
    variants = [base]
    without_parens = re.sub(r"\s*\([^)]*\)\s*$", "", base).strip()
    variants.append(without_parens)
    for token in _LABEL_SPLIT_TOKENS:
        if token in without_parens:
            variants.append(without_parens.split(token, 1)[0].strip())
    if without_parens.lower().startswith("the "):
        variants.append(without_parens[4:].strip())
    # Some names are written with a leading article-style prefix UNESCO uses in titles.
    for prefix in ("Historic Centre of ", "Historic City of ", "Old Town of ", "City of ",
                   "Historical Centre of ", "Old City of "):
        if without_parens.startswith(prefix):
            variants.append(without_parens[len(prefix):].strip())

    seen, out = set(), []
    for variant in variants:
        variant = variant.strip(" ,;:-–—")
        key = variant.lower()
        if len(variant) >= 4 and key not in seen:
            seen.add(key)
            out.append(variant)
    return out


def resolve_sites_by_name(unresolved, cache_path=None, languages=("en",)):
    """Fallback chain for sites whose P757 ID is not in Wikidata.

    `unresolved` is an iterable of (site_id, name_en, states) tuples.

    Pass 1 — Wikidata entity search on each name variant.
    Pass 2 — direct Wikipedia title lookup on each name variant, per language.

    cache_path: optional JSON file; already-resolved sites are skipped and results are
    written incrementally so an interrupted run can be resumed.
    """
    cache_path = Path(cache_path) if cache_path else None
    mapping = {}
    if cache_path and cache_path.exists():
        mapping = json.loads(cache_path.read_text())
        print(f"  [name fallback] resuming with {len(mapping)} cached entries")

    pending = [(str(sid), name, states) for sid, name, states in unresolved
               if str(sid) not in mapping]
    if not pending:
        return mapping

    def save():
        if cache_path:
            write_json_atomic(cache_path, mapping)

    for site_id, name, states in tqdm(pending, desc="Name fallback"):
        entry = None
        variants = name_variants(name)
        for variant in variants:
            entry = wikidata_entity_search(variant)
            time.sleep(SLEEP_WIKI)
            if entry:
                break
        if entry is None:
            for lang in languages:
                for variant in variants:
                    entry = wikipedia_direct_search(variant, lang=lang)
                    time.sleep(SLEEP_WIKI)
                    if entry:
                        break
                if entry:
                    break
        if entry:
            mapping[site_id] = entry
            if len(mapping) % 25 == 0:
                save()
    save()
    print(f"  [name fallback] resolved {len(mapping)} sites")
    return mapping


def articles_without_english(frame, mapping, id_col="site_id"):
    """The rows with no English Wikipedia article behind them, and why.

    Two kinds, told apart by the `status` column: `no article` for the ones nothing
    resolved at all, and `<lang> only` for the ones that landed on another edition. The
    resolver ranks English first, so the second kind means Wikidata lists no English
    sitelink for that item — usually genuine for a local site, occasionally the sign of
    a bad name match worth overriding.
    """
    rows = []
    for record in frame.drop_duplicates(id_col).to_dict("records"):
        entry = mapping.get(str(record[id_col])) or {}
        language = entry.get("wiki_language")
        if language == "en":
            continue
        rows.append({
            id_col: record[id_col],
            "label": record.get("label"),
            "states": record.get("states"),
            "status": f"{language} only" if entry.get("wiki_title") else "no article",
            "wiki_title": entry.get("wiki_title"),
            "wiki_lookup_source": entry.get("wiki_lookup_source"),
            "wikidata_url": entry.get("wikidata_url"),
        })
    return pd.DataFrame(rows, columns=[id_col, "label", "states", "status",
                                       "wiki_title", "wiki_lookup_source", "wikidata_url"])


def attach_wikidata_fields(frame, mapping, id_col="site_id"):
    """Attach wiki_* / wikidata_* columns onto a DataFrame from a site-ID mapping."""
    frame = frame.copy()
    keys = frame[id_col].astype(str)
    for field in WIKIDATA_FIELDS:
        frame[field] = keys.map(lambda k: (mapping.get(k) or {}).get(field))
    frame["wiki_lookup_source"] = keys.map(
        lambda k: (mapping.get(k) or {}).get("wiki_lookup_source")
    )
    return frame


# ══════════════════════════════════════════════════════════════════════════════
# 3b. Wikipedia article resolution — UNESCO Global Geoparks
#
# There is no geopark equivalent of P757. Wikidata has no UNESCO Global Geopark
# identifier property at all, and the one historical identifier it does carry,
# P2467 (Global Geoparks Network ID, former scheme), is on about 130 items, is
# formatted as "Portugal/6444", and also sits on national geoparks that were never
# UNESCO-designated — so it cannot be joined onto the export's `Internal ID`.
#
# The appropriate field to match on is therefore the name (`Titre EN`). The pass
# below still goes through Wikidata rather than straight to a text search: it pulls
# every item marked as a UNESCO Global Geopark — by designation (P1435), by class
# (P31), or by that former GGN identifier — with all of its labels and aliases, and
# matches our names against that closed set. Roughly 6 geoparks in 10 resolve this
# way, with far fewer false positives than an open-ended search. Whatever is left
# falls through to resolve_sites_by_name(), exactly as the World Heritage list does.
# ══════════════════════════════════════════════════════════════════════════════

# Words shared by every geopark name, in the languages Wikidata labels them in.
# Dropping them is what lets "Terres d'Hérault UNESCO Global Geopark" meet the
# Wikidata label "Géoparc mondial UNESCO des Terres d'Hérault".
_GEOPARK_NAME_NOISE = re.compile(
    r"\b(unesco|global|mondial|mundial|geopark|geoparks|geoparque|geoparc|"
    r"geoparco|geopargo|g[ée]oparc|natural|national)\b"
)


# Articles and prepositions left stranded once the programme words come out:
# "Géoparc mondial UNESCO des Terres d'Hérault" folds to "des terres d herault",
# which has to meet "terres d herault".
_GEOPARK_NAME_STOPWORDS = {
    "de", "des", "du", "da", "das", "do", "dos", "della", "delle", "del", "dei",
    "la", "le", "les", "el", "los", "las", "il", "lo", "of", "the", "van", "der",
    "und", "and", "et", "y", "e",
}


def geopark_name_key(name):
    """Fold a geopark name to a comparison key, with the programme words removed."""
    key = fold_for_search(strip_tags(name))
    key = _GEOPARK_NAME_NOISE.sub(" ", key)
    tokens = re.sub(r"[^a-z0-9]+", " ", key).split()
    while tokens and tokens[0] in _GEOPARK_NAME_STOPWORDS:
        tokens.pop(0)
    while tokens and tokens[-1] in _GEOPARK_NAME_STOPWORDS:
        tokens.pop()
    return " ".join(tokens)


def build_geopark_name_query(languages):
    """SPARQL: every Wikidata item marked a UNESCO Global Geopark, with its names."""
    langs = ",".join(f'"{lang}"' for lang in languages)
    return f"""
SELECT ?item ?name WHERE {{
  {{ ?item wdt:P1435 wd:{GEOPARK_DESIGNATION_QID} }}
  UNION {{ ?item wdt:P31 wd:{GEOPARK_DESIGNATION_QID} }}
  UNION {{ ?item wdt:P2467 ?ggn_id }}
  {{ ?item rdfs:label ?name }} UNION {{ ?item skos:altLabel ?name }}
  FILTER(LANG(?name) IN ({langs}))
}}
"""


def query_geopark_name_index(languages=("en", "fr", "es", "de", "it", "pt", "zh", "ja")):
    """Return {folded name → QID} for every Wikidata item marked a UNESCO Global Geopark.

    A name that two items answer to is dropped rather than guessed at.
    """
    data = _post_sparql(build_geopark_name_query(languages))
    rows = (data or {}).get("results", {}).get("bindings", [])
    index, ambiguous = {}, set()
    for row in rows:
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        key = geopark_name_key(row["name"]["value"])
        if not key:
            continue
        if index.get(key, qid) != qid:
            ambiguous.add(key)
        index[key] = qid
    for key in ambiguous:
        index.pop(key, None)
    print(f"  [geoparks] {len(set(index.values())):,} Wikidata items, "
          f"{len(index):,} usable names ({len(ambiguous)} ambiguous dropped)")
    return index


def query_wikidata_qids(qids, batch_size=200):
    """Return {QID → entry} with the best-ranked Wikipedia sitelink for each QID."""
    mapping = {}
    qids = list(dict.fromkeys(qids))
    for start in tqdm(range(0, len(qids), batch_size), desc="Sitelink batches"):
        batch = qids[start:start + batch_size]
        data = _post_sparql(build_sparql_qid_query(batch))
        if not data:
            continue
        for row in data["results"]["bindings"]:
            entry = _entry_from_sparql_row(row)
            entry["wiki_lookup_source"] = "wikidata_geopark_designation"
            _merge_entry(mapping, row["item"]["value"].rsplit("/", 1)[-1], entry)
        time.sleep(SLEEP_SPARQL)
    return mapping


def resolve_geoparks_by_designation(geoparks, name_index=None):
    """Resolve geoparks to Wikipedia articles through the designated-item name index.

    `geoparks` is an iterable of (geopark_id, name_en) pairs. Returns a mapping in the
    same shape as query_wikidata_batch(), keyed by the geopark's Internal ID.
    """
    if name_index is None:
        name_index = query_geopark_name_index()
    matched = {}
    for geopark_id, name in geoparks:
        qid = name_index.get(geopark_name_key(name))
        if qid:
            matched[str(geopark_id)] = qid
    print(f"  [geoparks] {len(matched):,} matched to a Wikidata item by name")
    sitelinks = query_wikidata_qids(matched.values()) if matched else {}
    mapping = {}
    for geopark_id, qid in matched.items():
        entry = sitelinks.get(qid)
        if entry:
            mapping[geopark_id] = dict(entry)
    print(f"  [geoparks] {len(mapping):,} of those have a Wikipedia article")
    return mapping


# ══════════════════════════════════════════════════════════════════════════════
# 4. Wikimedia pageviews
#
# Images are NOT fetched from Wikimedia. Every photo shown on the globe comes from the
# UNESCO export's own `Main Image` and `Images` columns; Wikipedia is used only to size
# the labels.
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# 4. Cross-check against Wikipedia's own list pages
#
# The resolution chain above works item by item, so a site whose Wikidata item lacks
# P757 or an English sitelink falls through it even when a perfectly good English
# article exists. Wikipedia's own curated lists catch exactly that case, and they are
# read from wikitext rather than rendered HTML: templates stay unexpanded, which keeps
# the hundreds of navbox links on a country page out of what gets parsed.
# ══════════════════════════════════════════════════════════════════════════════

WHC_LIST_PAGE = "List of World Heritage Sites by year of inscription"
GEOPARK_LIST_PAGES = [
    "UNESCO Global Geoparks",
    "List of UNESCO Global Geoparks in Africa",
    "List of UNESCO Global Geoparks in Asia",
    "List of UNESCO Global Geoparks in Europe",
    "List of UNESCO Global Geoparks in North America",
    "List of UNESCO Global Geoparks in Latin America",
]

# target, optional #section anchor, optional |display text
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_WHC_LIST_ID_RE = re.compile(r"whc\.unesco\.org/en/list/(\d+)")


def fetch_wikitext(page, project="en.wikipedia.org", retries=3):
    """The raw wikitext of one page, templates left unexpanded."""
    payload = _wikipedia_api(project, {"action": "parse", "page": page,
                                       "prop": "wikitext", "formatversion": 2},
                             retries=retries, label="wikitext")
    return ((payload or {}).get("parse") or {}).get("wikitext") or ""


def parse_whc_list(wikitext):
    """site_id -> English article, from the rows that carry both.

    Each row of the by-year table links the article and then the UNESCO record, as
    `[[Nahanni National Park Reserve]] || Natural || [https://whc.unesco.org/en/list/24 24]`,
    so the join is on the identifier and no name matching is involved. The country sits
    in a {{flag}} template rather than a link, which is why the row's first wikilink is
    reliably the site.
    """
    articles = {}
    for line in wikitext.splitlines():
        match = _WHC_LIST_ID_RE.search(line)
        if not match:
            continue
        links = [(target.strip(), bool(anchor))
                 for target, anchor, _ in _WIKILINK_RE.findall(line) if target.strip()]
        if links:
            target, section = links[0]
            articles.setdefault(int(match.group(1)), {"title": target, "section": section})
    return articles


def parse_geopark_list(wikitext, articles=None):
    """normalised geopark name -> English article, from table rows.

    These pages carry no identifier, so the join has to be on the name, and both halves
    of a link are indexed: the table writes `[[Fangshan District|Fangshan]]` where the
    export says "Fangshan UNESCO Global Geopark". Only table rows are read, and bare
    country names are skipped, so the prose and the country columns cannot supply a
    match.
    """
    articles = {} if articles is None else articles
    countries = {name.casefold() for name in COUNTRY_NAMES.values()}
    for line in wikitext.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        for target, anchor, display in _WIKILINK_RE.findall(line):
            target = target.strip()
            if not target or ":" in target:
                continue
            for name in (display or "", target):
                if not name or name.strip().casefold() in countries:
                    continue
                key = geopark_name_key(geopark_plain_name(name))
                if key and key not in articles:
                    articles[key] = {"title": target, "section": bool(anchor)}
    return articles


def _title_key(title):
    """Compare titles the way MediaWiki does: underscores are spaces, case is loose."""
    return clean_str(title).replace("_", " ").strip().casefold()


def wikidata_sitelinks(qids, batch_size=50):
    """qid -> {language: article title}, for the Wikipedia sitelinks of each item."""
    out = {}
    qids = [q for q in dict.fromkeys(qids) if q]
    for start in range(0, len(qids), batch_size):
        chunk = qids[start:start + batch_size]
        payload = _wikipedia_api("www.wikidata.org", {
            "action": "wbgetentities", "ids": "|".join(chunk), "props": "sitelinks",
        }, label="sitelinks")
        for qid, entity in ((payload or {}).get("entities") or {}).items():
            out[qid] = {site[:-4]: link["title"]
                        for site, link in (entity.get("sitelinks") or {}).items()
                        if site.endswith("wiki") and site != "commonswiki"}
        time.sleep(SLEEP_WIKI)
    return out


def inspect_resolution(site_ids, frame, mapping, id_col="site_id"):
    """Why each of these sites got the article it did.

    Shows the pick next to every Wikipedia the resolved Wikidata item actually links
    to. When the pick looks poor and `on_item` is just as poor, the item is the
    problem, not the ranking: Wikidata sometimes splits the inscription and its subject
    into two items, and each carries only part of the sitelinks.
    """
    wanted = {str(i) for i in site_ids}
    labels = {str(r[id_col]): r.get("label")
              for r in frame.drop_duplicates(id_col).to_dict("records")}
    entries = {key: mapping.get(key) or {} for key in wanted}
    qids = [(entry.get("wikidata_url") or "").rsplit("/", 1)[-1]
            for entry in entries.values() if entry.get("wikidata_url")]
    links = wikidata_sitelinks(qids)

    rows = []
    for key in sorted(wanted):
        entry = entries[key]
        qid = (entry.get("wikidata_url") or "").rsplit("/", 1)[-1] or None
        on_item = links.get(qid, {})
        best = min(on_item, key=article_rank) if on_item else None
        rows.append({
            id_col: key,
            "label": labels.get(key),
            "ours": entry.get("wiki_title"),
            "lang": entry.get("wiki_language"),
            "item": qid,
            "on_item": ", ".join(sorted(on_item, key=article_rank)) or "none",
            "best_on_item": best,
            "pick_is_best": (best == entry.get("wiki_language")) if best else None,
        })
    return pd.DataFrame(rows)


def crosscheck_articles(frame, mapping, listed, id_col="site_id", key=None):
    """Rows where the resolution chain and Wikipedia's list disagree.

    `listed` maps a join key to an English article; `key` derives that join key from a
    row, defaulting to the id. Only disagreements come back, each with a `status`:
    `no_article` when the chain found nothing, `other_language` when it found only a
    non-English edition, `different_article` when both name an English one and they
    differ. Agreement, and rows the list does not cover, are left out.
    """
    rows = []
    for record in frame.drop_duplicates(id_col).to_dict("records"):
        reference = listed.get(key(record) if key else record[id_col]) or {}
        title_listed = reference.get("title")
        if not title_listed:
            continue
        entry = mapping.get(str(record[id_col])) or {}
        title, language = entry.get("wiki_title"), entry.get("wiki_language")
        if language == "en" and _title_key(title) == _title_key(title_listed):
            continue
        if reference.get("section"):
            # The list points into a section of a broader article — "Ravenna" for the
            # Early Christian Monuments of Ravenna. A fair place to send a reader, but
            # the pageviews counted are the whole article's. If the chain already found
            # a dedicated English page, that one is the narrower of the two and stands.
            status = "ours_is_narrower" if language == "en" else "section_link"
        elif not title:
            status = "no_article"
        elif language != "en":
            status = "other_language"
        else:
            status = "different_article"
        rows.append({
            id_col: record[id_col],
            "label": record.get("label"),
            "status": status,
            "ours": title,
            "our_language": language,
            "wikipedia_lists": title_listed,
            "our_source": entry.get("wiki_lookup_source"),
        })
    order = {"no_article": 0, "other_language": 1, "different_article": 2,
             "section_link": 3, "ours_is_narrower": 4}
    frame = pd.DataFrame(rows, columns=[id_col, "label", "status", "ours",
                                        "our_language", "wikipedia_lists", "our_source"])
    if len(frame):
        frame = frame.sort_values("status", key=lambda c: c.map(order)).reset_index(drop=True)
    return frame


def apply_listed_articles(mapping, conflicts, statuses=(), ids=(), id_col="site_id"):
    """Point selected conflicts at the article Wikipedia's list names.

    Each title goes through wikipedia_direct_search, so a redirect resolves to its
    target and the entry carries the same fields the rest of the chain produces.
    Returns the ids actually changed.
    """
    statuses, ids = set(statuses), {str(i) for i in ids}
    changed = []
    if not len(conflicts):
        return changed
    selected = conflicts[conflicts["status"].isin(statuses)
                         | conflicts[id_col].astype(str).isin(ids)]
    for record in tqdm(selected.to_dict("records"), desc="Listed articles"):
        entry = wikipedia_direct_search(record["wikipedia_lists"], lang="en")
        time.sleep(SLEEP_WIKI)
        if not entry:
            note(f"  [crosscheck] no page for {record['wikipedia_lists']!r}")
            continue
        entry["wiki_lookup_source"] = "wikipedia_list"
        entry["wikidata_url"] = (mapping.get(str(record[id_col])) or {}).get("wikidata_url")
        mapping[str(record[id_col])] = entry
        changed.append(record[id_col])
    return changed


def get_pageviews(project, title, retries=4):
    """Return total Wikipedia views over the configured window for one project/title."""
    if not clean_str(title):
        return 0
    encoded = urllib.parse.quote(str(title), safe="")
    project = project or "en.wikipedia.org"
    url = f"{PAGEVIEWS_BASE}/{project}/all-access/{PAGEVIEWS_AGENT}/{encoded}/monthly/{START}/{END}"
    for _ in range(retries):
        try:
            response = requests.get(url, headers=wikimedia_headers(), timeout=20)
            if response.status_code == 404:
                return 0
            if response.status_code == 429:
                wait = wikimedia_retry_after(response, default=30)
                note(f"  [pageviews] 429 for {title!r} — waiting {wait}s")
                time.sleep(wait)
                continue
            if not response.ok:
                note(f"  [pageviews] HTTP {response.status_code} for {title!r}")
                return 0
            return sum(item["views"] for item in response.json().get("items", []))
        except Exception as exc:
            note(f"  [pageviews] error for {title!r}: {exc}")
            return 0
    note(f"  [pageviews] gave up after {retries} retries for {title!r}")
    return 0


def fetch_pageviews(articles, counts, cache_path, desc="Pageviews"):
    """Fill `counts` with the view total for every (project, title) it is missing.

    Both lists share one cache, so an article a geopark and a World Heritage property
    resolve to is only ever fetched once. The cache is flushed every 100 articles, so a
    run interrupted halfway keeps what it had. Returns the number actually fetched.
    """
    pending = [pair for pair in articles if f"{pair[0]}|{pair[1]}" not in counts]
    print(f"querying {len(pending):,} articles")
    for index, (project, title) in enumerate(tqdm(pending, desc=desc), start=1):
        counts[f"{project}|{title}"] = get_pageviews(project, title)
        time.sleep(SLEEP_PAGEVIEWS)
        if index % 100 == 0:
            write_json_atomic(cache_path, counts)
    write_json_atomic(cache_path, counts)
    return len(pending)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Photo delivery
#
# UNESCO publishes originals, not web assets. The 197 geopark photos alone come to
# 362 MB — mean 1.9 MB, 17 of them over 8 MB, the largest a 41 MB stereo JPEG — and
# neither host offers a resized variant: Azure Blob Storage ignores ?width= and the
# World Heritage Centre serves the file as uploaded. So the globe requests every photo
# through a resizing proxy, which is applied in index.html at render time rather than
# baked into sites.geojson: it then covers the 25,497 gallery URLs as well without
# adding ~1.3 MB of rewritten links to the file every visitor downloads, and each <img>
# can fall back to the original URL on its own if the proxy ever fails.
#
# What the proxy cannot do is ingest an image over 71 megapixels, and seven photos
# exceed that — two geoparks and five World Heritage properties, up to 128 megapixels.
# Those are the only images this module downloads: they are shrunk here, committed to
# the repo, and served directly.
# ══════════════════════════════════════════════════════════════════════════════

# ── Gap-filling: the lead photo of the Wikipedia article ──────────────────────
# The exports leave 56 sites with no photo at all (12 World Heritage properties, 44
# geoparks). For those, and only those, the globe borrows the lead photo of the site's
# Wikipedia article. It is the one place the pipeline takes an image from Wikimedia,
# and it needs two calls: `pageimages` for the thumbnail, then `imageinfo` on the file
# it names, because a Commons photo has to carry its author and licence.

WIKIPEDIA_THUMBNAIL_WIDTH = 760   # the width the popup asks the proxy for anyway


def _wikipedia_api(project, params, retries=3, label="wikipedia"):
    """GET the MediaWiki action API of one project, retrying on 429 and network errors."""
    url = f"https://{project}/w/api.php"
    for attempt in range(retries):
        try:
            response = requests.get(url, params={**params, "format": "json"},
                                    headers=wikimedia_headers(), timeout=20)
            if response.status_code == 429:
                wait = wikimedia_retry_after(response, default=2 ** attempt * 3)
                note(f"  [{label}] 429 — waiting {wait}s")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            if attempt == retries - 1:
                note(f"  [{label}] {exc}")
                return None
            time.sleep(2 ** attempt * 2)
    return None


def wikipedia_thumbnail(project, title, width=None, retries=3):
    """Return the article's lead photo and its credit, or None if it has no image.

    The credit comes from the Commons file description: `Artist` and `LicenseShortName`
    become the popup's credit line, `ObjectName` its caption when it is short enough to
    read as one.
    """
    title = clean_str(title)
    if not title:
        return None
    project = project or "en.wikipedia.org"
    width = width or WIKIPEDIA_THUMBNAIL_WIDTH

    payload = _wikipedia_api(project, {
        "action": "query", "titles": title, "redirects": 1,
        "prop": "pageimages", "piprop": "thumbnail|name", "pithumbsize": width,
    }, retries=retries, label="thumbnail")
    pages = ((payload or {}).get("query") or {}).get("pages") or {}
    page = next((p for key, p in pages.items() if key != "-1"), None)
    thumbnail = (page or {}).get("thumbnail") or {}
    if not thumbnail.get("source"):
        return None

    record = {
        # The API appends utm_* tracking parameters; they would only defeat caching.
        "image_url": thumbnail["source"].split("?", 1)[0],
        "image_width": thumbnail.get("width"),
        "image_author": None,
        "image_copyright": None,
        "image_caption": None,
        "image_source": "Wikipedia",
        "image_file": page.get("pageimage"),
        "image_article": page.get("title", title),
    }

    # Second call: the licence block on the file itself.
    if record["image_file"]:
        payload = _wikipedia_api(project, {
            "action": "query", "titles": f"File:{record['image_file']}",
            "prop": "imageinfo", "iiprop": "extmetadata",
            "iiextmetadatafilter": "Artist|LicenseShortName|ObjectName",
        }, retries=retries, label="thumbnail credit")
        pages = ((payload or {}).get("query") or {}).get("pages") or {}
        info = next((p.get("imageinfo") for p in pages.values() if p.get("imageinfo")), None)
        meta = (info[0].get("extmetadata") if info else None) or {}

        def field(name):
            return strip_tags((meta.get(name) or {}).get("value", ""))

        record["image_author"] = field("Artist") or None
        record["image_copyright"] = field("LicenseShortName") or None
        caption = field("ObjectName")
        record["image_caption"] = caption if 0 < len(caption) <= 120 else None
    return record


def fetch_wikipedia_thumbnails(sites, cache_path=None, width=None):
    """Look up a lead photo for each (key, project, title), caching by key.

    A site whose article has no image is cached as an empty record, so a re-run does
    not ask again. Returns the full cache, keyed by site id.
    """
    thumbnails = read_json_cache(cache_path) if cache_path else {}
    pending = [(str(key), project, title) for key, project, title in sites
               if str(key) not in thumbnails]
    print(f"looking up {len(pending):,} article lead photos")
    for index, (key, project, title) in enumerate(tqdm(pending, desc="Thumbnails"), start=1):
        thumbnails[key] = wikipedia_thumbnail(project, title, width=width) or {}
        time.sleep(SLEEP_WIKI)
        if cache_path and index % 25 == 0:
            write_json_atomic(cache_path, thumbnails)
    if cache_path and pending:
        write_json_atomic(cache_path, thumbnails)
    return thumbnails


IMAGE_PROXY_BASE = "https://wsrv.nl/"
# The popup is capped at 300 px, so the photo renders about 282 CSS px wide. 760 is
# therefore ~2.7x the display width — ample even at devicePixelRatio 2 — which makes
# quality, not width, the lever worth spending bytes on: q90 costs 1.43x q82 (62 kB ->
# 89 kB on a 10-photo sample) and keeps the card looking like a photograph.
IMAGE_PROXY_WIDTH = 760
IMAGE_PROXY_QUALITY = 90
IMAGE_PROXY_OUTPUT = "webp"

# Reduction targets for the handful the proxy refuses. These are the only photos served
# without the proxy in front of them, so they are cut once and kept generous: 1520 px is
# over 5x the rendered width, and q92 leaves no visible JPEG artefacts on a large screen.
LOCAL_IMAGE_MAX_EDGE = 1520
LOCAL_IMAGE_QUALITY = 92

# Pillow refuses an image over ~89 megapixels as a decompression bomb. The files here are
# UNESCO's own published photos, not untrusted uploads, so the ceiling is lifted for them.
LOCAL_IMAGE_PIXEL_CEILING = 400_000_000


def write_json_atomic(path, payload):
    """Write JSON via a temp file and one rename, so an interrupted run cannot truncate it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    os.replace(temporary, path)
    return path


def read_json_cache(path):
    """Read a JSON cache, tolerating a file left corrupt by an earlier interrupted run."""
    path = Path(path)
    if not path.exists():
        return {}
    raw = path.read_text()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # "Extra data": a shorter document written over a longer one. The leading document
        # is still valid and complete, so it is salvaged rather than thrown away.
        try:
            payload, _ = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            print(f"  [cache] {path} is unreadable — starting fresh")
            return {}
        print(f"  [cache] {path} had trailing junk — salvaged {len(payload):,} entries")
        write_json_atomic(path, payload)
        return payload


def proxy_image_url(url, width=None, quality=None, output=None):
    """Wrap a photo URL in the resizing proxy — the same URL index.html builds."""
    target = clean_str(url)
    if not target.lower().startswith(("http://", "https://")):
        return target
    params = urllib.parse.urlencode({
        "url": target,
        "w": IMAGE_PROXY_WIDTH if width is None else width,
        "output": IMAGE_PROXY_OUTPUT if output is None else output,
        "q": IMAGE_PROXY_QUALITY if quality is None else quality,
    })
    return f"{IMAGE_PROXY_BASE}?{params}"


def check_proxy_image(url, timeout=60):
    """HEAD one photo through the proxy. Returns {ok, status, bytes, message}."""
    try:
        response = requests.head(proxy_image_url(url), timeout=timeout,
                                 allow_redirects=True, headers=wikimedia_headers())
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "status": 0, "bytes": 0, "message": str(exc)[:120]}
    length = response.headers.get("Content-Length")
    return {
        "ok": response.status_code == 200,
        "status": response.status_code,
        "bytes": int(length) if (length or "").isdigit() else 0,
        "message": "" if response.status_code == 200 else response.reason or "",
    }


def check_proxy_images(urls, cache_path=None, workers=8):
    """HEAD many photos through the proxy in parallel, caching results on disk.

    A failure is cached too: the pixel limit is a property of the file, not a transient
    error, and re-checking 1,500 photos on every run is a waste. Delete the cache file to
    force a re-check after a new export.
    """
    cache_path = Path(cache_path) if cache_path else None
    results = read_json_cache(cache_path) if cache_path else {}
    if results:
        print(f"  [proxy] {len(results):,} cached checks from {cache_path}")

    pending = [u for u in dict.fromkeys(urls) if u and u not in results]
    if not pending:
        return results

    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(check_proxy_image, url): url for url in pending}
        for done in tqdm(futures.as_completed(jobs), total=len(jobs), desc="Proxy check"):
            results[jobs[done]] = done.result()

    if cache_path:
        write_json_atomic(cache_path, results)
    return results


def reduce_image_locally(url, dest_path, max_edge=None, quality=None, timeout=300):
    """Download one oversized photo and save a display-sized JPEG beside the globe.

    Returns the number of bytes written, or None if the photo could not be fetched or
    decoded. Pillow reads the first frame of a stereo JPEG (MPO), which is what a browser
    would show anyway.
    """
    from PIL import Image  # imported lazily: only this function needs Pillow

    max_edge = LOCAL_IMAGE_MAX_EDGE if max_edge is None else max_edge
    quality = LOCAL_IMAGE_QUALITY if quality is None else quality
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = LOCAL_IMAGE_PIXEL_CEILING
    try:
        response = requests.get(url, timeout=timeout, stream=True,
                                headers=wikimedia_headers())
        response.raise_for_status()
        with Image.open(io.BytesIO(response.content)) as image:
            source_size = image.size
            image = image.convert("RGB")
            image.thumbnail((max_edge, max_edge), Image.LANCZOS)
            image.save(dest_path, format="JPEG", quality=quality,
                       optimize=True, progressive=True)
    except (requests.exceptions.RequestException, OSError, ValueError) as exc:
        note(f"  [reduce] {type(exc).__name__}: {exc} — {url[:90]}")
        return None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    written = dest_path.stat().st_size
    megapixels = source_size[0] * source_size[1] / 1_000_000
    print(f"  [reduce] {source_size[0]}x{source_size[1]} ({megapixels:.0f} MP) "
          f"→ {dest_path} ({written / 1024:.0f} kB)")
    return written


# ══════════════════════════════════════════════════════════════════════════════
# 6. GeoJSON export
# ══════════════════════════════════════════════════════════════════════════════

# Properties written to sites.geojson. Anything else stays in the notebook's DataFrame.
GEOJSON_FIELDS = [
    "dataset", "site_id", "label", "short_label", "states", "iso_codes", "region",
    "category", "color_key", "in_danger", "date_inscribed", "criteria",
    "area_hectares", "short_description", "component_count",
    "unesco_url", "image_url", "extra_image_urls", "image_count",
    "image_author", "image_copyright", "image_caption", "image_source",
    "video_url", "website_url",
    "wiki_title", "wiki_language", "wiki_project", "wiki_url", "wikidata_url",
    "popularity", "label_rank", "label_count", "point_source",
    "cluster_component_count", "cluster_share",
]

INT_FIELDS = {"date_inscribed", "component_count", "popularity",
              "label_rank", "label_count", "cluster_component_count", "image_count"}
FLOAT_FIELDS = {"area_hectares", "cluster_share"}
BOOL_FIELDS = {"in_danger"}


def clean_json_value(value):
    """Normalise a value for JSON: NaN/NaT/empty become None, numpy scalars become Python."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def feature_properties(row):
    """Build the properties dict for one GeoJSON feature, with stable types."""
    props = {}
    for field in GEOJSON_FIELDS:
        value = clean_json_value(row.get(field))
        if value is None:
            continue
        if field == "site_id":
            # World Heritage properties are numbered; geoparks are keyed by their
            # alphanumeric Internal ID, which must survive as a string.
            text = str(value).strip()
            value = int(text) if text.isdigit() else text
        elif field in INT_FIELDS:
            try:
                value = int(round(float(value)))
            except (TypeError, ValueError):
                continue
        elif field in FLOAT_FIELDS:
            try:
                value = round(float(value), 4)
            except (TypeError, ValueError):
                continue
        elif field in BOOL_FIELDS:
            value = bool(value)
        elif field == "extra_image_urls" and isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        props[field] = value
    return props


def build_geojson(df, lat_col="lat", lon_col="lon"):
    """Turn the finished DataFrame into a GeoJSON FeatureCollection of Point features."""
    features = []
    for _, row in df.iterrows():
        lat, lon = row.get(lat_col), row.get(lon_col)
        if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(float(lon), 6), round(float(lat), 6)]},
            "properties": feature_properties(row),
        })
    return {"type": "FeatureCollection", "features": features}


def write_geojson(geojson, path):
    """Write a FeatureCollection to disk and report its size."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"Wrote {len(geojson['features']):,} features to {path} ({size_mb:.2f} MB)")
    return path


def fold_for_search(value):
    """Lowercase and strip accents — mirrors foldForSearch() in index.html."""
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn").lower()
    for src, dst in (("ø", "o"), ("ß", "ss"), ("ı", "i"), ("æ", "ae"), ("þ", "th")):
        text = text.replace(src, dst)
    return text
